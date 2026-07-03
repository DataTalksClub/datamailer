import pytest
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Client,
    Contact,
    EmailEvent,
    EmailTemplate,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.auth import create_client_api_key

pytestmark = pytest.mark.django_db(transaction=True)

API_KEY = "test-client-key"


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def api_client_record(organization):
    client = Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
        default_sender_id="newsletter",
        sender_emails=[{"id": "newsletter", "email": "newsletter@example.com"}],
    )
    create_client_api_key(client=client, name="Transactional test", raw_api_key=API_KEY)
    return client


@pytest.fixture
def template(api_client_record):
    return EmailTemplate.objects.create(
        client=api_client_record,
        key="email-verification",
        name="Email verification",
        subject="Verify {{ product }}",
        html_body="<p>Verify at {{ verification_url }}</p>",
        text_body="Verify at {{ verification_url }}",
    )


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def post_test_send(django_client, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_transactional_test_send"),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def test_test_send_renders_inline_without_sending_or_persisting(client, template, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_test_send(
        client,
        {
            "email": " Person@Example.COM ",
            "template_key": template.key,
            "idempotency_key": "verify-123",
            "context": {
                "product": "Datamailer",
                "verification_url": "https://example.com/verify/token",
            },
            "reply_to": "support@example.com",
            "cc": ["mentor@example.com"],
            "bcc": "audit@example.com",
        },
    )

    assert response.status_code == 200
    body = response.json()

    # Rendered email is returned inline.
    assert body["rendered"]["subject"] == "Verify Datamailer"
    assert body["rendered"]["html_body"] == "<p>Verify at https://example.com/verify/token</p>"
    assert body["rendered"]["text_body"] == "Verify at https://example.com/verify/token"
    assert body["would_deliver"] is True
    assert body["delivery_decision"] == {"allowed": True, "reason": ""}

    # Response is a superset of the real send response, but nothing was persisted.
    assert body["message"]["id"] is None
    assert body["message"]["created_at"] is None
    assert body["message"]["email"] == "person@example.com"
    assert body["message"]["from_email"] == "newsletter"
    assert body["message"]["from_email_address"] == "newsletter@example.com"
    assert body["message"]["reply_to"] == "support@example.com"
    assert body["message"]["cc"] == ["mentor@example.com"]
    assert body["message"]["bcc"] == ["audit@example.com"]
    assert body["message"]["status"] == TransactionalMessageStatus.QUEUED
    assert body["idempotent_replay"] is False
    assert body["enqueued"] is False

    # Nothing sent, nothing queued, no message/event rows created.
    assert enqueued == []
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0


def test_test_send_reports_suppression_but_still_renders(client, template, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    Contact.objects.create(
        email="bounced@example.com",
        normalized_email="bounced@example.com",
        hard_bounced_at=timezone.now(),
    )

    response = post_test_send(
        client,
        {
            "email": "bounced@example.com",
            "template_key": template.key,
            "context": {"product": "Datamailer", "verification_url": "https://example.com/v"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    # A real send would be rejected 409, but the preview still renders and just reports it.
    assert body["would_deliver"] is False
    assert body["delivery_decision"] == {"allowed": False, "reason": "hard_bounce"}
    assert body["message"]["status"] == TransactionalMessageStatus.SKIPPED
    assert body["rendered"]["subject"] == "Verify Datamailer"
    assert enqueued == []
    assert TransactionalMessage.objects.count() == 0


def test_test_send_requires_authentication(client, template):
    response = client.post(
        reverse("mailing:api_transactional_test_send"),
        data={"email": "a@example.com", "template_key": template.key},
        content_type="application/json",
    )
    assert response.status_code == 401


def test_test_send_validates_payload_like_real_send(client, template):
    response = post_test_send(client, {"template_key": template.key})
    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"email": "required"}


def test_test_send_returns_404_for_unknown_template(client, api_client_record):
    response = post_test_send(client, {"email": "a@example.com", "template_key": "does-not-exist"})
    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"template_key": "not_found"}
