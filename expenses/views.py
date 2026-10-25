import csv

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, HttpResponseBadRequest, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import ExpenseForm, GroupForm, OnboardingForm, QuickExpenseForm
from .models import Expense, Friendship, Payment, Profile, friends_of
from .services import (
    build_upi_uri,
    create_equal_split,
    get_balances,
    get_overview,
    is_valid_upi_id,
    qr_data_uri,
)

User = get_user_model()


def _csv_safe(value):
    """Neutralize spreadsheet formula prefixes in exported cells."""
    text = str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _posted_user(request):
    """Return the posted user, or None for missing and malformed IDs."""
    pk = request.POST.get("user", "")
    return get_object_or_404(User, pk=pk) if pk.isdigit() else None


def _expenses_for(user):
    return (
        Expense.objects.filter(group__members=user)
        .select_related("group", "paid_by__profile")
        .annotate(my_share=Sum("splits__amount", filter=Q(splits__user=user)))
        .order_by("-created_at")
    )


@login_required
def home(request):
    context = {
        **get_overview(request.user),
        "recent": _expenses_for(request.user)[:3],
    }
    return render(request, "expenses/home.html", context)


@login_required
@require_POST
def settle(request):
    other = _posted_user(request)
    if other is None or other == request.user:
        return HttpResponseBadRequest("Choose a valid other user.")
    overview = get_overview(request.user)
    receivable = dict(overview["to_get"])
    payable = dict(overview["to_give"])
    if other in receivable:
        Payment.objects.create(from_user=other, to_user=request.user, amount=receivable[other])
        messages.success(request, f"Marked ₹{receivable[other]:.2f} as received from {other.username}.")
    elif other in payable:
        Payment.objects.create(from_user=request.user, to_user=other, amount=payable[other])
        messages.success(request, f"Marked ₹{payable[other]:.2f} as paid to {other.username}.")
    return redirect("home")


@login_required
def pay(request, pk):
    """Show a validated UPI link only for a current, server-calculated debt."""
    other = get_object_or_404(User, pk=pk)
    amount = dict(get_overview(request.user)["to_give"]).get(other)
    if not amount:
        return redirect("home")
    prof = Profile.objects.filter(user=other).first()
    upi_id = prof.upi_id if prof else ""
    context = {
        "other": other,
        "amount": amount,
        "upi_id": upi_id,
        "upi_valid": is_valid_upi_id(upi_id),
    }
    if context["upi_valid"]:
        uri = build_upi_uri(upi_id, other.username, amount, f"Settle up - {request.user.username}")
        context.update(upi_uri=uri, qr=qr_data_uri(uri))
    return render(request, "expenses/pay.html", context)


@login_required
@transaction.atomic
def quick_expense(request):
    """Create an expense only in a group the current user belongs to."""
    groups = request.user.expense_groups.all().order_by("name")
    initial = {}
    group_id = request.GET.get("group", "")
    if group_id.isdigit() and groups.filter(pk=group_id).exists():
        initial["group"] = group_id
    form = QuickExpenseForm(request.POST or None, user=request.user, initial=initial)
    if form.is_valid():
        expense = form.save()
        create_equal_split(expense)
        messages.success(request, "Expense added and split across the group.")
        return redirect("group_detail", pk=expense.group_id)
    return render(request, "expenses/expense_form.html", {
        "form": form,
        "groups": groups,
        "selected_group": form.selected_group,
    })


@login_required
def history(request):
    expenses = _expenses_for(request.user)
    if "export" in request.GET:
        response = HttpResponse(
            content_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="expenses.csv"'},
        )
        writer = csv.writer(response)
        writer.writerow(["Date", "Group", "Description", "Paid by", "Amount", "Your share"])
        writer.writerows(
            (
                e.created_at.date(),
                _csv_safe(e.group),
                _csv_safe(e.description),
                _csv_safe(e.paid_by.username),
                e.amount,
                e.my_share or 0,
            )
            for e in expenses
        )
        return response
    return render(request, "expenses/history.html", {
        "page": Paginator(expenses, 15).get_page(request.GET.get("page")),
    })


@login_required
def group_list(request):
    groups = request.user.expense_groups.annotate(
        n_members=Count("members", distinct=True),
        n_expenses=Count("expenses", distinct=True),
    ).select_related("created_by")
    return render(request, "expenses/group_list.html", {"groups": groups})


@login_required
def group_create(request):
    form = GroupForm(request.POST or None, user=request.user)
    if form.is_valid():
        group = form.save(commit=False)
        group.created_by = request.user
        group.save()
        form.save_m2m()
        group.members.add(request.user)
        messages.success(request, f"{group.name} is ready.")
        return redirect("group_detail", pk=group.pk)
    return render(request, "expenses/group_form.html", {"form": form})


@login_required
@require_POST
def group_delete(request, pk):
    """Only the creator may delete; the confirmation is provided by the POST form."""
    group = get_object_or_404(request.user.created_groups, pk=pk)
    name = group.name
    group.delete()  # Cascades to this group's expenses and splits; global payments remain global.
    messages.success(request, f"{name} and its group expenses were deleted. Account-wide settlements were kept.")
    return redirect("group_list")


@login_required
@transaction.atomic
def group_detail(request, pk):
    group = get_object_or_404(request.user.expense_groups, pk=pk)
    form = ExpenseForm(request.POST or None, group=group)
    if form.is_valid():
        form.instance.group = group
        create_equal_split(form.save())
        messages.success(request, "Expense added and split equally.")
        return redirect("group_detail", pk=pk)
    expenses = group.expenses.select_related("paid_by__profile").order_by("-created_at")
    return render(request, "expenses/group_detail.html", {
        "group": group,
        "form": form,
        "expenses": expenses,
        "members": group.members.select_related("profile"),
        "balances": get_balances(group),
        "total": expenses.aggregate(Sum("amount"))["amount__sum"] or 0,
    })


@login_required
def profile(request):
    me = request.user
    prof, _ = Profile.objects.get_or_create(user=me)
    first_time = not prof.onboarded
    form = OnboardingForm(
        request.POST or None,
        request.FILES or None,
        user=me,
        initial={"username": "" if first_time else me.username, "upi_id": prof.upi_id},
    )
    if form.is_valid():
        me.username = form.cleaned_data["username"]
        me.save(update_fields=["username"])
        prof.avatar = form.cleaned_data["avatar"] or prof.avatar
        prof.upi_id = form.cleaned_data["upi_id"]
        prof.onboarded = True
        prof.save()
        request.session["onboarded"] = True
        messages.success(request, "Profile saved.")
        return redirect("friends" if first_time else "profile")
    stats = {
        "groups": me.expense_groups.count(),
        "friends": friends_of(me).count(),
        "paid": Expense.objects.filter(paid_by=me).aggregate(Sum("amount"))["amount__sum"] or 0,
    }
    return render(request, "expenses/profile.html", {
        "form": form,
        "first_time": first_time,
        "stats": stats,
    })


@login_required
def friend_profile(request, pk):
    """Reveal only basic profile details to an accepted friend."""
    if pk == request.user.pk:
        return redirect("profile")
    person = friends_of(request.user).filter(pk=pk, profile__onboarded=True).first()
    if person is None:
        raise Http404("Profile not available.")
    profile_obj = Profile.objects.filter(user=person).first()
    return render(request, "expenses/friend_profile.html", {
        "person": person,
        "friend_profile": profile_obj,
    })


@login_required
def friends(request):
    me = request.user
    if request.method == "POST":
        other, action = _posted_user(request), request.POST.get("action", "")
        if other is None or other == me:
            return HttpResponseBadRequest("Choose a valid other user.")
        if action == "add":
            eligible = Profile.objects.filter(user=other, onboarded=True).exists()
            if not eligible:
                return HttpResponseBadRequest("This account is not ready to connect.")
            if not Friendship.between(me, other).exists():
                Friendship.objects.create(from_user=me, to_user=other)
                messages.success(request, f"Friend request sent to {other.username}.")
        elif action == "accept":
            updated = Friendship.objects.filter(
                from_user=other, to_user=me, accepted=False
            ).update(accepted=True)
            if updated:
                messages.success(request, f"You and {other.username} are now friends.")
        elif action == "remove":
            Friendship.between(me, other).filter(accepted=True).delete()
            messages.success(request, f"Removed {other.username} from your friends.")
        elif action == "cancel":
            Friendship.objects.filter(
                from_user=me, to_user=other, accepted=False
            ).delete()
            messages.success(request, f"Cancelled your request to {other.username}.")
        else:
            return HttpResponseBadRequest("Unknown friend action.")
        return redirect("friends")

    q = request.GET.get("q", "").strip()
    people = (
        User.objects.filter(profile__onboarded=True)
        .exclude(Q(pk=me.pk) | Q(friend_requests_sent__to_user=me) | Q(friend_requests_received__from_user=me))
        .select_related("profile")
        .order_by("username")
    )
    if q:
        people = people.filter(username__icontains=q)
    people_page = Paginator(people, 20).get_page(request.GET.get("page"))
    return render(request, "expenses/friends.html", {
        "q": q,
        "people": people_page,
        "results": people_page.object_list if q else [],
        "friends": friends_of(me),
        "incoming": Friendship.objects.filter(
            to_user=me, accepted=False
        ).select_related("from_user__profile").order_by("from_user__username"),
        "outgoing": Friendship.objects.filter(
            from_user=me, accepted=False
        ).select_related("to_user__profile").order_by("to_user__username"),
    })
