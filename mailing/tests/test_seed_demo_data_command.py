import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    EmailValidationStatus,
    Organization,
    Subscription,
    SubscriptionStatus,
    Tag,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.auth import check_api_key

pytestmark = pytest.mark.django_db


COUNTED_MODELS = [
    Organization,
    Audience,
    Client,
    Tag,
    Contact,
    Subscription,
    Campaign,
    CampaignRecipient,
    EmailTemplate,
    TransactionalMessage,
    EmailEvent,
]


def model_counts():
    return {model.__name__: model.objects.count() for model in COUNTED_MODELS}


def test_seed_demo_data_is_idempotent_and_creates_local_admin():
    call_command("seed_demo_data")
    first_counts = model_counts()
    call_command("seed_demo_data")
    second_counts = model_counts()

    assert second_counts == first_counts
    assert first_counts == {
        "Organization": 2,
        "Audience": 3,
        "Client": 3,
        "Tag": 9,
        "Contact": 12,
        "Subscription": 16,
        "Campaign": 4,
        "CampaignRecipient": 12,
        "EmailTemplate": 4,
        "TransactionalMessage": 6,
        "EmailEvent": 21,
    }

    admin = get_user_model().objects.get(username="admin")
    assert admin.email == "admin@example.com"
    assert admin.is_staff is True
    assert admin.is_superuser is True
    assert admin.check_password("admin") is True


def test_seed_demo_data_creates_representative_campaign_and_contact_states():
    call_command("seed_demo_data")

    assert set(Campaign.objects.values_list("status", flat=True)) == {
        CampaignStatus.DRAFT,
        CampaignStatus.QUEUED,
        CampaignStatus.SENDING,
        CampaignStatus.SENT,
    }
    assert set(CampaignRecipient.objects.values_list("status", flat=True)) == {
        CampaignRecipientStatus.PENDING,
        CampaignRecipientStatus.SENT,
        CampaignRecipientStatus.SKIPPED,
        CampaignRecipientStatus.FAILED,
        CampaignRecipientStatus.BOUNCED,
        CampaignRecipientStatus.COMPLAINED,
        CampaignRecipientStatus.UNSUBSCRIBED,
    }

    sent_campaign = Campaign.objects.get(subject="Demo Sent: Weekly Roundup")
    assert sent_campaign.delivered_count == 4
    assert sent_campaign.unique_open_count == 1
    assert sent_campaign.unique_click_count == 1
    assert sent_campaign.unsubscribe_count == 1
    assert sent_campaign.bounce_count == 1
    assert sent_campaign.complaint_count == 1
    assert EmailEvent.objects.filter(campaign=sent_campaign, event_type=EmailEventType.OPEN).exists()
    assert EmailEvent.objects.filter(campaign=sent_campaign, event_type=EmailEventType.CLICK).exists()
    assert EmailEvent.objects.filter(campaign=sent_campaign, event_type=EmailEventType.UNSUBSCRIBE).exists()
    assert EmailEvent.objects.filter(campaign=sent_campaign, event_type=EmailEventType.BOUNCE).exists()
    assert EmailEvent.objects.filter(campaign=sent_campaign, event_type=EmailEventType.COMPLAINT).exists()

    verified = Contact.objects.get(normalized_email="alex.verified@example.com")
    unverified = Contact.objects.get(normalized_email="bailey.unverified@example.com")
    invalid = Contact.objects.get(normalized_email="lina.failed@example.com")
    global_unsubscribed = Contact.objects.get(normalized_email="casey.global-unsub@example.com")
    hard_bounced = Contact.objects.get(normalized_email="drew.hard-bounce@example.com")
    complained = Contact.objects.get(normalized_email="erin.complaint@example.com")
    multi = Contact.objects.get(normalized_email="harper.multi@example.com")

    assert verified.verified_at is not None
    assert verified.email_validation_status == EmailValidationStatus.VALID
    assert verified.email_validated_at is not None
    assert unverified.verified_at is None
    assert unverified.email_validation_status == EmailValidationStatus.UNKNOWN
    assert unverified.email_validated_at is None
    assert invalid.email_validation_status == EmailValidationStatus.MANUALLY_INVALID
    assert invalid.email_validation_reason == "demo manual hygiene review"
    assert global_unsubscribed.global_unsubscribed_at is not None
    assert hard_bounced.hard_bounced_at is not None
    assert complained.complained_at is not None
    assert multi.subscriptions.values("audience", "client").distinct().count() == 3
    assert set(multi.tags.values_list("slug", flat=True)) == {"newsletter", "events", "founder"}

    client_unsubscribed = Subscription.objects.get(contact__normalized_email="fatima.client-unsub@example.com")
    assert client_unsubscribed.status == SubscriptionStatus.UNSUBSCRIBED


def test_seed_demo_data_creates_transactional_history_and_hashed_api_keys():
    call_command("seed_demo_data")

    client = Client.objects.get(slug="dtc-courses")
    assert check_api_key("demo-dtc-courses-api-key", client.api_key_hash) is True
    assert client.api_key_hash != "demo-dtc-courses-api-key"

    assert set(EmailTemplate.objects.values_list("key", flat=True)) == {
        "email-verification",
        "password-reset",
        "registration-welcome",
        "workshop-reminder",
    }
    verification_template = EmailTemplate.objects.get(key="email-verification")
    assert verification_template.required_context == [
        {"name": "name", "description": "Recipient display name."},
        {"name": "verification_url", "description": "Client-generated verification URL."},
    ]
    assert verification_template.example_context == {
        "name": "Alex",
        "verification_url": "https://client.example/verify/placeholder",
    }
    assert set(TransactionalMessage.objects.values_list("status", flat=True)) == {
        TransactionalMessageStatus.QUEUED,
        TransactionalMessageStatus.SENT,
        TransactionalMessageStatus.BOUNCED,
        TransactionalMessageStatus.COMPLAINED,
        TransactionalMessageStatus.SKIPPED,
        TransactionalMessageStatus.FAILED,
    }
    sent_message = TransactionalMessage.objects.get(idempotency_key="demo-sent")
    assert sent_message.delivered_at is not None
    assert sent_message.first_opened_at is not None
    assert sent_message.first_clicked_at is not None
    assert EmailEvent.objects.filter(transactional_message=sent_message, event_type=EmailEventType.DELIVERED).exists()
