from django.urls import path

from mailing import views

app_name = "mailing"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("health/", views.health, name="health"),
    path("api/v1/contacts", views.api_contacts, name="api_contacts"),
    path("api/v1/contacts/status", views.api_contact_status, name="api_contact_status"),
    path("api/v1/subscriptions/subscribe", views.api_subscribe, name="api_subscribe"),
    path("api/v1/subscriptions/unsubscribe", views.api_unsubscribe, name="api_unsubscribe"),
    path("api/v1/transactional/send", views.api_transactional_send, name="api_transactional_send"),
]
