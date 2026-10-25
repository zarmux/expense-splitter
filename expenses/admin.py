from django.contrib import admin

from .models import Expense, Friendship, Group, Payment, Profile, Split


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ("name", "created_by", "created_at")
    filter_horizontal = ("members",)


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("description", "group", "paid_by", "amount", "created_at")


admin.site.register([Split, Profile, Friendship, Payment])
