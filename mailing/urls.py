from django.urls import path

from mailing import views

app_name = "mailing"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("health/", views.health, name="health"),
]
