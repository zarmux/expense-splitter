from collections import defaultdict
from decimal import Decimal, ROUND_DOWN

from .models import Split


def create_equal_split(expense):
    members = list(expense.group.members.all())
    share = (expense.amount / len(members)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    remainder = expense.amount - share * len(members)

    for index, member in enumerate(members):
        amount = share + remainder if index == 0 else share
        Split.objects.create(expense=expense, user=member, amount=amount)


def get_balances(group):
    balances = defaultdict(Decimal)
    for member in group.members.all():
        balances[member] = Decimal("0.00")
    for expense in group.expenses.prefetch_related("splits"):
        balances[expense.paid_by] += expense.amount
        for split in expense.splits.all():
            balances[split.user] -= split.amount
    return dict(balances)