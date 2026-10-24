from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ExpenseForm
from .services import create_equal_split, get_balances


@login_required
def group_list(request):
    groups = request.user.expense_groups.all()
    return render(request, "expenses/group_list.html", {"groups": groups})


@login_required
def group_detail(request, pk):
    group = get_object_or_404(request.user.expense_groups, pk=pk)

    if request.method == "POST":
        form = ExpenseForm(request.POST, group=group)
        if form.is_valid():
            expense = form.save(commit=False)
            expense.group = group
            expense.save()
            create_equal_split(expense)
            return redirect("group_detail", pk=group.pk)
    else:
        form = ExpenseForm(group=group)

    context = {
        "group": group,
        "form": form,
        "expenses": group.expenses.order_by("-created_at"),
        "balances": get_balances(group),
    }
    return render(request, "expenses/group_detail.html", context)