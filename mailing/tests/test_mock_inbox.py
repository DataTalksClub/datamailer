import boto3
import pytest
from botocore.stub import Stubber
from django.test import override_settings
from django.urls import reverse

from mailing.models import (
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.auth import create_client_api_key
from mailing.services.mock_inbox import is_mock_address
from mailing.services.transactional import build_transactional_queue_payload
from mailing.services.transactional_sender import send_transactional_email_from_queue

pytestmark = pytest.mark.django_db(transaction=True)

API_KEY = "mock-inbox-key"

MOCK_SETTINGS = {
    "MOCK_INBOX_ENABLED": True,
    "MOCK_INBOX_DOMAIN": "mailbox.test",
    "MOCK_INBOX_PLUS_TAG": "e2e",
}


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
    create_client_api_key(client=client, name="Mock inbox test", raw_api_key=API_KEY)
    return client


@pytest.fixture
def other_client(organization):
    client = Client.objects.create(
        organization=organization,
        name="Other",
        slug="other",
        default_sender_id="newsletter",
        sender_emails=[{"id": "newsletter", "email": "newsletter@example.com"}],
    )
    create_client_api_key(client=client, name="Other", raw_api_key="other-key")
    return client


@pytest.fixture
def template(api_client_record):
    return EmailTemplate.objects.create(
        client=api_client_record,
        key="homework-confirmation",
        name="Homework confirmation",
        subject="Submission received",
        html_body="<p>Thanks</p>",
        text_body="Thanks",
    )


def make_message(client, template, email, *, status=TransactionalMessageStatus.SENT, idempotency_key=""):
    contact, _ = Contact.objects.get_or_create(normalized_email=email.strip().casefold(), defaults={"email": email})
    return TransactionalMessage.objects.create(
        client=client,
        contact=contact,
        email=contact.normalized_email,
        from_email_id="newsletter",
        from_email="newsletter@example.com",
        template=template,
        template_key=template.key,
        status=status,
        idempotency_key=idempotency_key,
        subject="Submission received",
        html_body="<p>Thanks</p>",
        text_body="Thanks",
        context={"course_slug": "e2e-smoke"},
        metadata={"event": "homework_submission"},
    )


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


# --- address recognition ---------------------------------------------------


@override_settings(**MOCK_SETTINGS)
@pytest.mark.parametrize(
    "email,expected",
    [
        ("anyone@mailbox.test", True),
        ("ANYONE@Mailbox.Test", True),
        ("e2e@example.com", True),
        ("e2e+homework@example.com", True),
        ("e2e+project-2026@datatalks.club", True),
        ("student@example.com", False),
        ("e2eish@example.com", False),
        ("not-an-email", False),
    ],
)
def test_is_mock_address(email, expected):
    assert is_mock_address(email) is expected


@override_settings(MOCK_INBOX_ENABLED=False, MOCK_INBOX_DOMAIN="mailbox.test", MOCK_INBOX_PLUS_TAG="e2e")
def test_is_mock_address_disabled():
    assert is_mock_address("anyone@mailbox.test") is False


# --- list / detail / clear API ---------------------------------------------


@override_settings(**MOCK_SETTINGS)
def test_list_messages_for_address(client, api_client_record, template):
    make_message(api_client_record, template, "e2e+a@mailbox.test", idempotency_key="k1")
    make_message(api_client_record, template, "e2e+a@mailbox.test", idempotency_key="k2")
    make_message(api_client_record, template, "e2e+other@mailbox.test", idempotency_key="k3")

    response = client.get(
        reverse("mailing:api_mock_inbox_messages"),
        {"address": "e2e+a@mailbox.test"},
        **auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["address"] == "e2e+a@mailbox.test"
    assert body["count"] == 2
    subjects = {m["subject"] for m in body["messages"]}
    assert subjects == {"Submission received"}
    assert body["messages"][0]["template_key"] == "homework-confirmation"


@override_settings(**MOCK_SETTINGS)
def test_list_is_client_scoped(client, api_client_record, other_client, template):
    make_message(api_client_record, template, "e2e+a@mailbox.test", idempotency_key="k1")

    response = client.get(
        reverse("mailing:api_mock_inbox_messages"),
        {"address": "e2e+a@mailbox.test"},
        **auth_headers("other-key"),
    )
    assert response.status_code == 200
    assert response.json()["count"] == 0


@override_settings(**MOCK_SETTINGS)
def test_list_rejects_non_mock_address(client, api_client_record, template):
    response = client.get(
        reverse("mailing:api_mock_inbox_messages"),
        {"address": "real-student@example.com"},
        **auth_headers(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["fields"] == {"address": "not_a_mock_address"}


@override_settings(**MOCK_SETTINGS)
def test_message_detail_includes_bodies_and_context(client, api_client_record, template):
    message = make_message(api_client_record, template, "e2e+detail@mailbox.test", idempotency_key="k1")

    response = client.get(
        reverse("mailing:api_mock_inbox_message_detail", args=[message.id]),
        **auth_headers(),
    )
    assert response.status_code == 200
    detail = response.json()["message"]
    assert detail["html_body"] == "<p>Thanks</p>"
    assert detail["context"] == {"course_slug": "e2e-smoke"}
    assert detail["metadata"] == {"event": "homework_submission"}


@override_settings(**MOCK_SETTINGS)
def test_message_detail_hidden_for_non_mock(client, api_client_record, template):
    message = make_message(api_client_record, template, "real@example.com", idempotency_key="k1")
    response = client.get(
        reverse("mailing:api_mock_inbox_message_detail", args=[message.id]),
        **auth_headers(),
    )
    assert response.status_code == 404


@override_settings(**MOCK_SETTINGS)
def test_clear_by_address(client, api_client_record, template):
    make_message(api_client_record, template, "e2e+a@mailbox.test", idempotency_key="k1")
    make_message(api_client_record, template, "e2e+b@mailbox.test", idempotency_key="k2")

    response = client.delete(
        reverse("mailing:api_mock_inbox_messages"),
        data='{"address": "e2e+a@mailbox.test"}',
        content_type="application/json",
        **auth_headers(),
    )
    assert response.status_code == 200
    assert response.json()["deleted_count"] == 1
    assert TransactionalMessage.objects.filter(email="e2e+b@mailbox.test").exists()
    assert not TransactionalMessage.objects.filter(email="e2e+a@mailbox.test").exists()


@override_settings(**MOCK_SETTINGS)
def test_clear_all_mock_messages_only(client, api_client_record, template):
    make_message(api_client_record, template, "e2e+a@mailbox.test", idempotency_key="k1")
    make_message(api_client_record, template, "real@example.com", idempotency_key="k2")

    response = client.delete(reverse("mailing:api_mock_inbox_messages"), **auth_headers())
    assert response.status_code == 200
    assert response.json()["deleted_count"] == 1
    assert TransactionalMessage.objects.filter(email="real@example.com").exists()
    assert not TransactionalMessage.objects.filter(email="e2e+a@mailbox.test").exists()


@override_settings(**MOCK_SETTINGS)
def test_requires_auth(client):
    response = client.get(reverse("mailing:api_mock_inbox_messages"), {"address": "e2e@mailbox.test"})
    assert response.status_code == 401


@override_settings(MOCK_INBOX_ENABLED=False)
def test_disabled_returns_404(client, api_client_record):
    response = client.get(
        reverse("mailing:api_mock_inbox_messages"),
        {"address": "e2e@mailbox.test"},
        **auth_headers(),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "mock_inbox_disabled"


# --- worker: real delivery is skipped for mock addresses -------------------


@override_settings(**MOCK_SETTINGS)
def test_worker_captures_without_ses(api_client_record, template):
    message = make_message(
        api_client_record,
        template,
        "e2e+worker@mailbox.test",
        status=TransactionalMessageStatus.QUEUED,
        idempotency_key="worker-1",
    )
    payload = build_transactional_queue_payload(message)

    ses = boto3.client("ses", region_name="us-east-1")
    with Stubber(ses) as stubber:
        # No SES calls are queued: any call would raise StubAssertionError.
        send_transactional_email_from_queue(payload, client=ses)
        stubber.assert_no_pending_responses()

    message.refresh_from_db()
    assert message.status == TransactionalMessageStatus.SENT
    assert message.ses_message_id == f"mock-inbox:{message.id}"
    sent_event = EmailEvent.objects.get(transactional_message=message, event_type=EmailEventType.SENT)
    assert sent_event.metadata.get("mock_inbox") is True
