from django.urls import path

from mailing import views

app_name = "mailing"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("health/", views.health, name="health"),
    path("t/o/<str:tracking_token>.gif", views.tracking_open, name="tracking_open"),
    path("t/c/<str:tracking_token>", views.tracking_click, name="tracking_click"),
    path("unsubscribe/<str:unsubscribe_token>", views.public_unsubscribe, name="public_unsubscribe"),
    path("webhooks/ses", views.ses_webhook, name="ses_webhook"),
    path("api/v1/contacts", views.api_contacts, name="api_contacts"),
    path("api/v1/contacts/status", views.api_contact_status, name="api_contact_status"),
    path("api/v1/subscriptions/subscribe", views.api_subscribe, name="api_subscribe"),
    path("api/v1/subscriptions/unsubscribe", views.api_unsubscribe, name="api_unsubscribe"),
    path("api/v1/transactional/send", views.api_transactional_send, name="api_transactional_send"),
]
