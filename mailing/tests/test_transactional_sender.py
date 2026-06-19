import json

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber
from django.test import override_settings
from django.utils import timezone

from mailing.models import (
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
from mailing.services.transactional import build_transactional_queue_payload
from mailing.sqs import records_from_messages
from mailing.workers import transactional_email_handler

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def api_client_record(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def contact():
    return Contact.objects.create(email="person@example.com")


@pytest.fixture
def template(api_client_record):
    return EmailTemplate.objects.create(
        client=api_client_record,
        key="email-verification",
        name="Email verification",
        subject="Mutable template subject",
        html_body="<p>Mutable template body</p>",
        text_body="Mutable template body",
    )


@pytest.fixture
def transactional_message(api_client_record, contact, template):
    return TransactionalMessage.objects.create(
        client=api_client_record,
        contact=contact,
        email=contact.normalized_email,
        from_email_id="courses",
        from_email="courses@dtcdev.click",
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.QUEUED,
        idempotency_key="verify-123",
        subject="Persisted subject",
        html_body="<p>Persisted body</p>",
        text_body="Persisted body",
    )


class CallbackResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def collect_cmp_callbacks(monkeypatch):
    posts = []

    def fake_urlopen(request, *, timeout):
        posts.append(
            {
                "url": request.full_url,
                "json": json.loads(request.data.decode("utf-8")),
                "headers": dict(request.header_items()),
                "timeout": timeout,
            }
        )
        return CallbackResponse()

    monkeypatch.setattr("mailing.services.cmp_callbacks.urlopen", fake_urlopen)
    return posts


@override_settings(DEFAULT_FROM_EMAIL="sender@example.com", AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_transactional_handler_sends_persisted_message_and_records_sent_event(transactional_message, monkeypatch):
    ses = boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(ses) as stubber:
        stubber.add_response(
            "send_email",
            {"MessageId": "ses-message-123"},
            {
                "Source": "courses@dtcdev.click",
                "Destination": {"ToAddresses": ["person@example.com"]},
                "Message": {
                    "Subject": {"Charset": "UTF-8", "Data": "Persisted subject"},
                    "Body": {
                        "Html": {"Charset": "UTF-8", "Data": "<p>Persisted body</p>"},
                        "Text": {"Charset": "UTF-8", "Data": "Persisted body"},
                    },
                },
            },
        )
        monkeypatch.setattr("mailing.services.transactional_sender.ses_client", lambda: ses)

        response = transactional_email_handler(
            _event("message-1", build_transactional_queue_payload(transactional_message))
        )

    transactional_message.refresh_from_db()
    event = EmailEvent.objects.get(event_type=EmailEventType.SENT)
    assert response == {"batchItemFailures": []}
    assert transactional_message.status == TransactionalMessageStatus.SENT
    assert transactional_message.ses_message_id == "ses-message-123"
    assert transactional_message.sent_at is not None
    assert transactional_message.last_error == ""
    assert event.transactional_message == transactional_message
    assert event.contact == transactional_message.contact
    assert event.client == transactional_message.client
    assert event.metadata["ses_message_id"] == "ses-message-123"


@override_settings(DEFAULT_FROM_EMAIL="sender@example.com", AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_transactional_handler_uses_message_display_sender(transactional_message, monkeypatch):
    transactional_message.from_email_id = "courses"
    transactional_message.from_email = "DataTalks.Club Courses <courses@dtcdev.click>"
    transactional_message.save(update_fields=["from_email_id", "from_email", "updated_at"])
    ses = boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(ses) as stubber:
        stubber.add_response(
            "send_email",
            {"MessageId": "ses-message-123"},
            {
                "Source": "DataTalks.Club Courses <courses@dtcdev.click>",
                "Destination": {"ToAddresses": ["person@example.com"]},
                "Message": {
                    "Subject": {"Charset": "UTF-8", "Data": "Persisted subject"},
                    "Body": {
                        "Html": {"Charset": "UTF-8", "Data": "<p>Persisted body</p>"},
                        "Text": {"Charset": "UTF-8", "Data": "Persisted body"},
                    },
                },
            },
        )
        monkeypatch.setattr("mailing.services.transactional_sender.ses_client", lambda: ses)

        response = transactional_email_handler(
            _event("message-1", build_transactional_queue_payload(transactional_message))
        )

    transactional_message.refresh_from_db()
    assert response == {"batchItemFailures": []}
    assert transactional_message.status == TransactionalMessageStatus.SENT
    assert transactional_message.ses_message_id == "ses-message-123"


@pytest.mark.parametrize(
    ("status", "event_type"),
    [
        (TransactionalMessageStatus.SENT, EmailEventType.SENT),
        (TransactionalMessageStatus.SKIPPED, EmailEventType.SKIPPED),
        (TransactionalMessageStatus.BOUNCED, EmailEventType.BOUNCE),
        (TransactionalMessageStatus.COMPLAINED, EmailEventType.COMPLAINT),
    ],
)
def test_duplicate_terminal_delivery_is_acknowledged_without_ses_or_duplicate_event(
    transactional_message,
    monkeypatch,
    status,
    event_type,
):
    transactional_message.status = status
    update_fields = ["status", "updated_at"]
    if status == TransactionalMessageStatus.SENT:
        transactional_message.ses_message_id = "already-sent"
        transactional_message.sent_at = timezone.now()
        update_fields += ["ses_message_id", "sent_at"]
    transactional_message.save(update_fields=update_fields)
    EmailEvent.objects.create(
        transactional_message=transactional_message,
        contact=transactional_message.contact,
        client=transactional_message.client,
        event_type=event_type,
        metadata={},
    )

    def fail_if_called():
        raise AssertionError("SES should not be called for terminal duplicate deliveries")

    monkeypatch.setattr("mailing.services.transactional_sender.ses_client", fail_if_called)

    response = transactional_email_handler(
        _event("message-1", build_transactional_queue_payload(transactional_message))
    )

    assert response == {"batchItemFailures": []}
    assert EmailEvent.objects.filter(event_type=event_type).count() == 1


def test_client_or_idempotency_mismatch_marks_failed_and_acknowledges(transactional_message, monkeypatch):
    payload = build_transactional_queue_payload(transactional_message) | {
        "client_id": transactional_message.client_id + 1
    }

    def fail_if_called():
        raise AssertionError("SES should not be called for queue payload mismatches")

    monkeypatch.setattr("mailing.services.transactional_sender.ses_client", fail_if_called)

    response = transactional_email_handler(_event("message-1", payload))

    transactional_message.refresh_from_db()
    event = EmailEvent.objects.get(event_type=EmailEventType.FAILED)
    assert response == {"batchItemFailures": []}
    assert transactional_message.status == TransactionalMessageStatus.FAILED
    assert "client_id/idempotency_key" in transactional_message.last_error
    assert event.metadata["reason"] == "queue_payload_mismatch"


def test_transient_ses_failure_leaves_message_retryable_and_returns_batch_failure(transactional_message, monkeypatch):
    class TransientSesClient:
        def send_email(self, **params):
            raise EndpointConnectionError(endpoint_url="https://email.us-east-1.amazonaws.com")

    monkeypatch.setattr("mailing.services.transactional_sender.ses_client", lambda: TransientSesClient())

    response = transactional_email_handler(
        _event("message-1", build_transactional_queue_payload(transactional_message))
    )

    transactional_message.refresh_from_db()
    assert response == {"batchItemFailures": [{"itemIdentifier": "message-1"}]}
    assert transactional_message.status == TransactionalMessageStatus.QUEUED
    assert transactional_message.ses_message_id == ""
    assert "Could not connect" in transactional_message.last_error
    assert EmailEvent.objects.count() == 0


@override_settings(CMP_WEBHOOK_URL="https://cmp.example.com/api/datamailer/events", CMP_WEBHOOK_TOKEN="secret")
def test_permanent_ses_failure_marks_failed_and_acknowledges(transactional_message, monkeypatch):
    posts = collect_cmp_callbacks(monkeypatch)

    class PermanentSesClient:
        def send_email(self, **params):
            raise ClientError(
                {"Error": {"Code": "MessageRejected", "Message": "Address rejected"}},
                "SendEmail",
            )

    monkeypatch.setattr("mailing.services.transactional_sender.ses_client", lambda: PermanentSesClient())

    response = transactional_email_handler(
        _event("message-1", build_transactional_queue_payload(transactional_message))
    )

    transactional_message.refresh_from_db()
    event = EmailEvent.objects.get(event_type=EmailEventType.FAILED)
    assert response == {"batchItemFailures": []}
    assert transactional_message.status == TransactionalMessageStatus.FAILED
    assert transactional_message.ses_message_id == ""
    assert transactional_message.last_error == "MessageRejected: Address rejected"
    assert event.metadata["reason"] == "ses_permanent_failure"
    assert CmpCallback.objects.filter(status=CmpCallbackStatus.PENDING).count() == 1
    process_due_cmp_callbacks()
    assert len(posts) == 1
    assert posts[0]["url"] == "https://cmp.example.com/api/datamailer/events"
    assert posts[0]["headers"]["Authorization"] == "Bearer secret"
    assert posts[0]["json"]["event_type"] == "transactional.failed"
    assert posts[0]["json"]["email"] == transactional_message.email
    assert posts[0]["json"]["client"] == transactional_message.client.slug
    assert posts[0]["json"]["metadata"]["reason"] == "ses_permanent_failure"


def test_mixed_batch_retries_only_invalid_and_transient_records(transactional_message, monkeypatch):
    class TransientSesClient:
        def send_email(self, **params):
            raise EndpointConnectionError(endpoint_url="https://email.us-east-1.amazonaws.com")

    monkeypatch.setattr("mailing.services.transactional_sender.ses_client", lambda: TransientSesClient())
    valid_payload = build_transactional_queue_payload(transactional_message)
    invalid_payload = valid_payload | {"version": 999}
    event = records_from_messages(
        [
            _message("transient-message", valid_payload),
            _message("invalid-message", invalid_payload),
        ]
    )

    response = transactional_email_handler(event)

    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "transient-message"},
            {"itemIdentifier": "invalid-message"},
        ]
    }


def test_missing_message_row_is_returned_for_retry(transactional_message):
    payload = build_transactional_queue_payload(transactional_message)
    transactional_message.delete()

    response = transactional_email_handler(_event("message-1", payload))

    assert response == {"batchItemFailures": [{"itemIdentifier": "message-1"}]}


def _event(message_id, payload):
    return records_from_messages([_message(message_id, payload)])


def _message(message_id, payload):
    return {
        "MessageId": message_id,
        "ReceiptHandle": f"{message_id}-receipt",
        "Body": json.dumps(payload),
    }
