import base64
import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    CmpCallback,
    CmpCallbackStatus,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.cmp_callbacks import process_due_cmp_callbacks
from mailing.services.contacts import is_marketing_email_allowed, is_transactional_email_allowed
from mailing.services.ses_webhooks import SNS_MOCK_SIGNATURE, canonical_sns_message
from mailing.sqs import records_from_messages
from mailing.workers import ses_webhooks_handler

pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def app_client(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def contact():
    return Contact.objects.create(email="person@example.com", verified_at=timezone.now())


@pytest.fixture
def campaign(audience, app_client):
    return Campaign.objects.create(audience=audience, client=app_client, subject="Weekly update")


@pytest.fixture
def recipient(campaign, contact):
    return CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        ses_message_id="ses-campaign-1",
        sent_at=timezone.now(),
    )


@pytest.fixture
def transactional_message(app_client, contact):
    template = EmailTemplate.objects.create(
        client=app_client,
        key="password-reset",
        name="Password reset",
        subject="Reset",
        is_transactional=True,
    )
    return TransactionalMessage.objects.create(
        client=app_client,
        contact=contact,
        email=contact.email,
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.SENT,
        idempotency_key="tx-1",
        subject="Reset",
        ses_message_id="ses-tx-1",
        sent_at=timezone.now(),
    )


def test_webhook_endpoint_enqueues_valid_notification_without_mutating_state(client, recipient, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.ses_webhooks.enqueue_ses_webhook", lambda payload: enqueued.append(payload))

    response = client.post(
        reverse("mailing:ses_webhook"),
        data=json.dumps(sns_payload(ses_payload("Delivery", "ses-campaign-1"), message_id="sns-delivery-1")),
        content_type="application/json",
    )

    recipient.refresh_from_db()
    assert response.status_code == 200
    assert response.json()["enqueued"] is True
    assert recipient.delivered_at is None
    assert EmailEvent.objects.count() == 0
    assert enqueued == [
        {
            "contract": "ses-webhooks",
            "version": 1,
            "provider": "ses",
            "provider_event_id": "sns-delivery-1",
            "notification_type": "delivery",
            "received_at": enqueued[0]["received_at"],
            "ses_message_id": "ses-campaign-1",
            "mail_message_id": "ses-campaign-1",
            "metadata": {
                "sns_message_id": "sns-delivery-1",
                "sns_topic_arn": "arn:aws:sns:us-east-1:123456789012:ses",
                "mail_timestamp": "2026-05-24T12:00:00Z",
                "mail_source": "newsletter@example.com",
            },
        }
    ]


def test_webhook_endpoint_rejects_invalid_signature_without_enqueue_or_mutation(client, recipient, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.ses_webhooks.enqueue_ses_webhook", lambda payload: enqueued.append(payload))
    payload = sns_payload(ses_payload("Delivery", "ses-campaign-1"), signature="wrong")

    response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    recipient.refresh_from_db()
    assert response.status_code == 403
    assert enqueued == []
    assert recipient.delivered_at is None
    assert EmailEvent.objects.count() == 0


def test_webhook_endpoint_handles_subscription_confirmation_only_when_allowed(client, monkeypatch):
    calls = []

    class FakeResponse:
        def __enter__(self):
            calls.append("confirmed")
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr("mailing.services.ses_webhooks.urlopen", lambda url, timeout: FakeResponse())
    payload = sns_payload({}, message_type="SubscriptionConfirmation") | {
        "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription",
        "Token": "token-1",
    }

    skipped = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    with override_settings(SES_WEBHOOKS_ALLOW_SUBSCRIPTION_CONFIRMATION=True):
        confirmed = client.post(
            reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json"
        )

    assert skipped.status_code == 200
    assert skipped.json()["confirmed"] is False
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed"] is True
    assert calls == ["confirmed"]


@override_settings(SES_WEBHOOKS_SIGNATURE_MODE="strict")
def test_strict_sns_signature_validation_uses_certificate_rules(client, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = build_test_certificate(key)
    payload = sns_payload(ses_payload("Delivery", "ses-unknown"), signature="") | {
        "SignatureVersion": "2",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem",
    }
    signature = key.sign(canonical_sns_message(payload).encode(), padding.PKCS1v15(), hashes.SHA256())
    payload["Signature"] = base64.b64encode(signature).decode()
    enqueued = []
    monkeypatch.setattr("mailing.services.ses_webhooks.fetch_signing_certificate", lambda url: cert)
    monkeypatch.setattr("mailing.services.ses_webhooks.enqueue_ses_webhook", lambda payload: enqueued.append(payload))

    response = client.post(reverse("mailing:ses_webhook"), data=json.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    assert enqueued[0]["provider_event_id"] == payload["MessageId"]


def test_webhook_endpoint_rejects_malformed_and_unsupported_payloads(client):
    malformed = client.post(reverse("mailing:ses_webhook"), data="{bad-json", content_type="application/json")
    unsupported = client.post(
        reverse("mailing:ses_webhook"),
        data=json.dumps(sns_payload({}, message_type="UnsubscribeConfirmation")),
        content_type="application/json",
    )

    assert malformed.status_code == 400
    assert unsupported.status_code == 400
    assert EmailEvent.objects.count() == 0


def test_worker_delivery_updates_campaign_recipient_and_stats_idempotently(recipient):
    payload = webhook_payload("delivery", "sns-delivery-1", "ses-campaign-1")
    event = records_from_payloads([("first", payload), ("duplicate", payload)])

    response = ses_webhooks_handler(event, None)

    recipient.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert recipient.delivered_at is not None
    assert recipient.status == CampaignRecipientStatus.SENT
    assert recipient.campaign.delivered_count == 1
    assert EmailEvent.objects.filter(event_type=EmailEventType.DELIVERED, campaign_recipient=recipient).count() == 1


def test_worker_raw_sns_delivery_updates_campaign_recipient(recipient):
    event = records_from_payloads(
        [("message-1", sns_payload(ses_payload("Delivery", "ses-campaign-1"), message_id="sns-raw-delivery"))]
    )

    response = ses_webhooks_handler(event, None)

    recipient.refresh_from_db()
    recipient.campaign.refresh_from_db()
    event = EmailEvent.objects.get()
    assert response == {"batchItemFailures": []}
    assert recipient.delivered_at is not None
    assert recipient.campaign.delivered_count == 1
    assert event.event_type == EmailEventType.DELIVERED
    assert event.provider_event_id == "sns-raw-delivery"
    assert event.metadata["sns_message_id"] == "sns-raw-delivery"


def test_worker_delivery_updates_transactional_message(transactional_message):
    response = ses_webhooks_handler(
        records_from_payloads([("message-1", webhook_payload("delivery", "sns-tx-delivery", "ses-tx-1"))]),
        None,
    )

    transactional_message.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert transactional_message.delivered_at is not None
    assert transactional_message.status == TransactionalMessageStatus.SENT
    assert (
        EmailEvent.objects.filter(
            event_type=EmailEventType.DELIVERED,
            transactional_message=transactional_message,
        ).count()
        == 1
    )


def test_worker_hard_bounce_suppresses_campaign_contact(recipient, audience, app_client):
    payload = webhook_payload(
        "bounce",
        "sns-bounce-1",
        "ses-campaign-1",
        metadata={"bounce_type": "Permanent", "bounce_sub_type": "General"},
    )

    ses_webhooks_handler(records_from_payloads([("message-1", payload)]), None)

    recipient.refresh_from_db()
    recipient.contact.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.BOUNCED
    assert recipient.contact.hard_bounced_at is not None
    assert recipient.campaign.bounce_count == 1
    assert is_marketing_email_allowed(recipient.contact, audience, app_client) is False
    assert is_transactional_email_allowed(recipient.contact) is False
    assert EmailEvent.objects.filter(event_type=EmailEventType.BOUNCE, campaign_recipient=recipient).count() == 1


@override_settings(CMP_WEBHOOK_URL="https://cmp.example.com/api/datamailer/events", CMP_WEBHOOK_TOKEN="secret")
def test_worker_hard_bounce_emits_cmp_callback(recipient, monkeypatch):
    posts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_urlopen(request, *, timeout):
        posts.append(
            {
                "url": request.full_url,
                "json": json.loads(request.data.decode("utf-8")),
                "headers": dict(request.header_items()),
                "timeout": timeout,
            }
        )
        return Response()

    monkeypatch.setattr(
        "mailing.services.cmp_callbacks.urlopen",
        fake_urlopen,
    )
    monkeypatch.setattr(
        "mailing.services.cmp_callbacks.transaction.on_commit",
        lambda callback: callback(),
    )
    payload = webhook_payload(
        "bounce",
        "sns-bounce-cmp",
        "ses-campaign-1",
        metadata={"bounce_type": "Permanent", "bounce_sub_type": "General"},
    )

    ses_webhooks_handler(records_from_payloads([("message-1", payload)]), None)
    assert CmpCallback.objects.filter(status=CmpCallbackStatus.PENDING).count() == 1
    process_due_cmp_callbacks()

    assert len(posts) == 1
    body = posts[0]["json"]
    assert posts[0]["url"] == "https://cmp.example.com/api/datamailer/events"
    assert posts[0]["headers"]["Authorization"] == "Bearer secret"
    assert body["event_type"] == "contact.hard_bounced"
    assert body["email"] == recipient.contact.normalized_email
    assert body["audience"] == recipient.campaign.audience.slug
    assert body["client"] == recipient.campaign.client.slug
    assert body["metadata"]["bounce_type"] == "Permanent"


def test_worker_raw_sns_hard_bounce_suppresses_campaign_contact(recipient, audience, app_client):
    event = records_from_payloads(
        [
            (
                "message-1",
                sns_payload(
                    ses_payload(
                        "Bounce",
                        "ses-campaign-1",
                        detail={"bounceType": "Permanent", "bounceSubType": "General"},
                    ),
                    message_id="sns-raw-bounce",
                ),
            )
        ]
    )

    response = ses_webhooks_handler(event, None)

    recipient.refresh_from_db()
    recipient.contact.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert recipient.status == CampaignRecipientStatus.BOUNCED
    assert recipient.contact.hard_bounced_at is not None
    assert recipient.campaign.bounce_count == 1
    assert is_marketing_email_allowed(recipient.contact, audience, app_client) is False
    assert is_transactional_email_allowed(recipient.contact) is False
    assert EmailEvent.objects.filter(event_type=EmailEventType.BOUNCE, campaign_recipient=recipient).count() == 1


def test_worker_complaint_suppresses_transactional_contact(transactional_message):
    payload = webhook_payload("complaint", "sns-complaint-1", "ses-tx-1")

    ses_webhooks_handler(records_from_payloads([("message-1", payload)]), None)

    transactional_message.refresh_from_db()
    transactional_message.contact.refresh_from_db()
    assert transactional_message.status == TransactionalMessageStatus.COMPLAINED
    assert transactional_message.contact.complained_at is not None
    assert is_transactional_email_allowed(transactional_message.contact) is False
    assert (
        EmailEvent.objects.filter(
            event_type=EmailEventType.COMPLAINT,
            transactional_message=transactional_message,
        ).count()
        == 1
    )


def test_worker_raw_sns_complaint_suppresses_transactional_contact(transactional_message):
    event = records_from_payloads(
        [
            (
                "message-1",
                sns_payload(
                    ses_payload("Complaint", "ses-tx-1", detail={"complaintFeedbackType": "abuse"}),
                    message_id="sns-raw-complaint",
                ),
            )
        ]
    )

    response = ses_webhooks_handler(event, None)

    transactional_message.refresh_from_db()
    transactional_message.contact.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert transactional_message.status == TransactionalMessageStatus.COMPLAINED
    assert transactional_message.contact.complained_at is not None
    assert is_transactional_email_allowed(transactional_message.contact) is False
    assert (
        EmailEvent.objects.filter(
            event_type=EmailEventType.COMPLAINT,
            transactional_message=transactional_message,
        ).count()
        == 1
    )


def test_worker_soft_bounce_audits_without_hard_suppression(recipient):
    payload = webhook_payload(
        "bounce",
        "sns-soft-bounce-1",
        "ses-campaign-1",
        metadata={"bounce_type": "Transient", "bounce_sub_type": "MailboxFull"},
    )

    ses_webhooks_handler(records_from_payloads([("message-1", payload)]), None)

    recipient.refresh_from_db()
    recipient.contact.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.SENT
    assert recipient.last_error == "soft_bounce"
    assert recipient.contact.hard_bounced_at is None
    assert EmailEvent.objects.filter(event_type=EmailEventType.BOUNCE, campaign_recipient=recipient).count() == 1


def test_worker_open_and_click_update_summary_fields_without_duplicate_unique_counts(recipient):
    payloads = [
        ("open-1", webhook_payload("open", "sns-open-1", "ses-campaign-1")),
        ("open-dup", webhook_payload("open", "sns-open-1", "ses-campaign-1")),
        ("click-1", webhook_payload("click", "sns-click-1", "ses-campaign-1", metadata={"url": "https://example.com"})),
    ]

    ses_webhooks_handler(records_from_payloads(payloads), None)

    recipient.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert recipient.open_count == 1
    assert recipient.click_count == 1
    assert recipient.first_opened_at is not None
    assert recipient.first_clicked_at is not None
    assert recipient.campaign.open_count == 1
    assert recipient.campaign.unique_open_count == 1
    assert recipient.campaign.click_count == 1
    assert recipient.campaign.unique_click_count == 1
    assert EmailEvent.objects.filter(campaign_recipient=recipient).count() == 2


def test_worker_uncorrelated_message_id_records_diagnostics_without_suppression(contact):
    payload = webhook_payload("bounce", "sns-unknown-bounce", "missing-ses-message")

    response = ses_webhooks_handler(records_from_payloads([("message-1", payload)]), None)

    contact.refresh_from_db()
    event = EmailEvent.objects.get()
    assert response == {"batchItemFailures": []}
    assert event.campaign_recipient_id is None
    assert event.transactional_message_id is None
    assert event.metadata["ses_message_id"] == "missing-ses-message"
    assert contact.hard_bounced_at is None


def test_worker_returns_partial_batch_failures_for_bad_records(recipient):
    event = records_from_payloads(
        [
            ("valid", webhook_payload("delivery", "sns-delivery-2", "ses-campaign-1")),
            ("invalid", webhook_payload("delivery", "sns-bad", "ses-campaign-1") | {"version": 999}),
        ]
    )

    response = ses_webhooks_handler(event, None)

    recipient.refresh_from_db()
    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid"}]}
    assert recipient.delivered_at is not None


def test_worker_raw_sns_invalid_envelope_fails_only_that_batch_item(recipient):
    valid = sns_payload(ses_payload("Delivery", "ses-campaign-1"), message_id="sns-raw-delivery-2")
    invalid = sns_payload(ses_payload("Delivery", "ses-campaign-1"), message_id="sns-raw-invalid") | {
        "Message": "{not-json",
    }
    event = records_from_payloads([("valid", valid), ("invalid", invalid)])

    response = ses_webhooks_handler(event, None)

    recipient.refresh_from_db()
    assert response == {"batchItemFailures": [{"itemIdentifier": "invalid"}]}
    assert recipient.delivered_at is not None
    assert EmailEvent.objects.filter(provider_event_id="sns-raw-delivery-2").count() == 1
    assert EmailEvent.objects.filter(provider_event_id="sns-raw-invalid").count() == 0


def sns_payload(message, *, message_id="sns-message-1", message_type="Notification", signature=SNS_MOCK_SIGNATURE):
    return {
        "Type": message_type,
        "MessageId": message_id,
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:ses",
        "Message": json.dumps(message),
        "Timestamp": "2026-05-24T12:00:01Z",
        "SignatureVersion": "1",
        "Signature": signature,
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-test.pem",
    }


def ses_payload(event_type, ses_message_id, *, detail=None):
    return {
        "eventType": event_type,
        "mail": {
            "timestamp": "2026-05-24T12:00:00Z",
            "source": "newsletter@example.com",
            "messageId": ses_message_id,
        },
        event_type.casefold(): detail or {},
    }


def webhook_payload(notification_type, provider_event_id, ses_message_id, *, metadata=None):
    return {
        "contract": "ses-webhooks",
        "version": 1,
        "provider": "ses",
        "provider_event_id": provider_event_id,
        "notification_type": notification_type,
        "received_at": "2026-05-24T12:00:01Z",
        "ses_message_id": ses_message_id,
        "metadata": {"mail_timestamp": "2026-05-24T12:00:00Z"} | (metadata or {}),
    }


def records_from_payloads(message_payloads):
    return records_from_messages(
        [
            {
                "MessageId": message_id,
                "ReceiptHandle": f"{message_id}-receipt",
                "Body": json.dumps(payload),
            }
            for message_id, payload in message_payloads
        ]
    )


def build_test_certificate(private_key):
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.us-east-1.amazonaws.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(dt_timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(dt_timezone.utc) + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)
