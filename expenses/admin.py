from django.contrib import admin
from .models import Group, Expense, Split

admin.site.register(Group)
admin.site.register(Expense)
admin.site.register(Split)