import re
from collections import defaultdict
from decimal import Decimal
from urllib.parse import quote

import segno
from django.contrib.auth import get_user_model
from django.db.models import Sum

from .models import Payment, Split

# A UPI ID looks like  name@bankhandle  (for example rahul@okhdfcbank).
UPI_ID_RE = re.compile(r"^[A-Za-z0-9._-]{2,256}@[A-Za-z][A-Za-z0-9]{1,64}$")


def is_valid_upi_id(value):
    return bool(UPI_ID_RE.match(value or ""))


def build_upi_uri(upi_id, payee_name, amount, note):
    """The link that opens a UPI app (GPay, PhonePe, Paytm...) with everything filled in.
    Every piece is URL-encoded so a name can't smuggle extra parameters into the link."""
    amount = Decimal(amount).quantize(Decimal("0.01"))
    return (
        "upi://pay"
        f"?pa={quote(upi_id, safe='@')}"
        f"&pn={quote(payee_name)}"
        f"&am={amount}"
        "&cu=INR"  # UPI only supports rupees
        f"&tn={quote(note[:50])}"
    )


def qr_data_uri(text):
    """A QR code as an inline PNG, so a phone can scan it from a computer screen."""
    return segno.make(text, error="m").png_data_uri(scale=6, border=2)


def create_equal_split(expense):
    if expense.amount <= 0:
        raise ValueError("Expense amount must be greater than zero.")
    members = list(expense.group.members.all())
    if not members:
        raise ValueError("A group needs at least one member to split an expense.")
    share, extra = divmod(int(expense.amount * 100), len(members))
    Split.objects.bulk_create(
        Split(expense=expense, user=m, amount=Decimal(share + (i < extra)) / 100)
        for i, m in enumerate(members)
    )


def get_balances(group):
    paid = dict(group.expenses.values_list("paid_by").annotate(Sum("amount")))
    owed = dict(Split.objects.filter(expense__group=group).values_list("user").annotate(Sum("amount")))
    return {m: paid.get(m.id, 0) - owed.get(m.id, 0) for m in group.members.select_related("profile")}


def get_overview(user):
    """Net money between you and each person across all groups (positive: they owe you)."""
    net = defaultdict(Decimal)
    for qs, key, sign in (
        (Split.objects.filter(expense__paid_by=user).exclude(user=user), "user", 1),
        (Split.objects.filter(user=user).exclude(expense__paid_by=user), "expense__paid_by", -1),
        (Payment.objects.filter(from_user=user), "to_user", 1),
        (Payment.objects.filter(to_user=user), "from_user", -1),
    ):
        for person, total in qs.values_list(key).annotate(Sum("amount")):
            net[person] += sign * total
    people = get_user_model().objects.select_related("profile").in_bulk([p for p, t in net.items() if t])
    rows = sorted(((people[p], t) for p, t in net.items() if t), key=lambda r: -abs(r[1]))
    to_get = [(p, t) for p, t in rows if t > 0]
    to_give = [(p, -t) for p, t in rows if t < 0]
    return {
        "to_get": to_get, "to_give": to_give,
        "total_get": sum(t for _, t in to_get), "total_give": sum(t for _, t in to_give),
    }