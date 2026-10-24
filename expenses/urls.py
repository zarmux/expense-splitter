from django.urls import path
from . import views

urlpatterns = [
    path("", views.group_list, name="group_list"),
    path("groups/<int:pk>/", views.group_detail, name="group_detail"),
]