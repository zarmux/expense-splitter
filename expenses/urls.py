from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("groups/", views.group_list, name="group_list"),
    path("groups/new/", views.group_create, name="group_create"),
    path("groups/<int:pk>/", views.group_detail, name="group_detail"),
    path("groups/<int:pk>/delete/", views.group_delete, name="group_delete"),
    path("expenses/new/", views.quick_expense, name="quick_expense"),
    path("history/", views.history, name="history"),
    path("friends/", views.friends, name="friends"),
    path("friends/<int:pk>/", views.friend_profile, name="friend_profile"),
    path("profile/", views.profile, name="profile"),
    path("welcome/", views.profile, name="welcome"),
    path("settle/", views.settle, name="settle"),
    path("pay/<int:pk>/", views.pay, name="pay"),
]
