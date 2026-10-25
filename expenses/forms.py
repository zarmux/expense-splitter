from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model

from .models import Expense, Group, friends_of
from .services import is_valid_upi_id

User = get_user_model()
MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB


class ExpenseForm(forms.ModelForm):
    amount = forms.DecimalField(min_value=Decimal("0.01"), max_digits=10, decimal_places=2)

    class Meta:
        model = Expense
        fields = ["description", "amount", "paid_by"]

    def __init__(self, *args, group, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["paid_by"].queryset = group.members.all().order_by("username")


class QuickExpenseForm(forms.ModelForm):
    group = forms.ModelChoiceField(queryset=Group.objects.none(), label="Group")
    amount = forms.DecimalField(min_value=Decimal("0.01"), max_digits=10, decimal_places=2)

    class Meta:
        model = Expense
        fields = ["group", "description", "amount", "paid_by"]

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.user_groups = user.expense_groups.all().order_by("name")
        self.fields["group"].queryset = self.user_groups
        self.fields["group"].empty_label = "Choose a group"
        self.fields["group"].widget.attrs["onchange"] = "if (this.value) window.location.search = '?group=' + encodeURIComponent(this.value);"
        self.fields["paid_by"].queryset = (
            User.objects.filter(expense_groups__in=self.user_groups).distinct().order_by("username")
        )
        self.selected_group = None
        raw_group = self.data.get("group") if self.is_bound else self.initial.get("group")
        if raw_group and str(raw_group).isdigit():
            self.selected_group = self.user_groups.filter(pk=raw_group).first()
        if self.selected_group:
            self.fields["paid_by"].queryset = self.selected_group.members.all().order_by("username")
            if not self.is_bound and self.selected_group.members.filter(pk=user.pk).exists():
                self.initial.setdefault("paid_by", user.pk)

    def clean(self):
        cleaned = super().clean()
        group = cleaned.get("group")
        payer = cleaned.get("paid_by")
        if group and payer and not group.members.filter(pk=payer.pk).exists():
            self.add_error("paid_by", "Choose someone who belongs to this group.")
        return cleaned


class GroupForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = ["name", "members"]
        widgets = {"members": forms.CheckboxSelectMultiple}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["members"].queryset = friends_of(user).order_by("username")
        self.fields["members"].required = False


class OnboardingForm(forms.Form):
    username = forms.RegexField(
        r"^\w{3,30}$",
        error_messages={"invalid": "Use 3–30 letters, numbers or _."},
    )
    avatar = forms.ImageField(required=False)
    upi_id = forms.CharField(required=False, max_length=60, label="UPI ID (optional)")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_upi_id(self):
        upi_id = self.cleaned_data["upi_id"].strip()
        if upi_id and not is_valid_upi_id(upi_id):
            raise forms.ValidationError("Enter a valid UPI ID, like name@okhdfcbank.")
        return upi_id

    def clean_avatar(self):
        avatar = self.cleaned_data.get("avatar")
        if avatar and avatar.size > MAX_AVATAR_BYTES:
            raise forms.ValidationError("Please choose a photo under 2 MB.")
        return avatar

    def clean_username(self):
        name = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=name).exclude(pk=self.user.pk).exists():
            raise forms.ValidationError("That username is already in use.")
        return name
