import json

import pytest
from django.test import override_settings
from django.urls import reverse

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CapturedEmail,
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
from mailing.services.campaign_sender import send_campaign_batch
from mailing.services.transactional import build_transactional_queue_payload
from mailing.sqs import records_from_messages
from mailing.workers import transactional_email_handler

pytestmark = pytest.mark.django_db(transaction=True)

API_KEY = "capture-api-key"


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def api_client_record(organization):
    client = Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
    )
    create_client_api_key(client=client, name="Capture test", raw_api_key=API_KEY)
    return client


@pytest.fixture
def audience(organization):
    return Audience.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
    )


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


@override_settings(
    DATAMAILER_DELIVERY_MODE="capture",
    DATAMAILER_CAPTURE_UI=True,
    MOCK_INBOX_ENABLED=False,
)
def test_transactional_worker_captures_without_ses(api_client_record, monkeypatch):
    contact = Contact.objects.create(email="learner@example.com")
    template = EmailTemplate.objects.create(
        client=api_client_record,
        key="registration-confirmation",
        name="Registration confirmation",
        subject="Welcome",
        html_body="<p>Hello</p>",
        text_body="Hello",
    )
    message = TransactionalMessage.objects.create(
        client=api_client_record,
        contact=contact,
        email=contact.normalized_email,
        from_email="courses@dtcdev.click",
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.QUEUED,
        idempotency_key="registration:42",
        subject="Rendered subject",
        html_body="<p>Rendered</p>",
        text_body="Rendered",
        metadata={"source": "course-management-platform", "event": "registration"},
    )

    def fail_if_called():
        raise AssertionError("SES should not be called")

    monkeypatch.setattr("mailing.services.transactional_sender.ses_client", fail_if_called)

    response = transactional_email_handler(
        _event("message-1", build_transactional_queue_payload(message))
    )

    message.refresh_from_db()
    capture = CapturedEmail.objects.get(transactional_message=message)
    event = EmailEvent.objects.get(transactional_message=message, event_type=EmailEventType.SENT)
    assert response == {"batchItemFailures": []}
    assert message.status == TransactionalMessageStatus.SENT
    assert message.ses_message_id == f"capture:{capture.id}"
    assert capture.email == "learner@example.com"
    assert capture.subject == "Rendered subject"
    assert capture.html_body == "<p>Rendered</p>"
    assert capture.source == "course-management-platform"
    assert capture.event == "registration"
    assert capture.metadata["delivery_mode"] == "capture"
    assert event.metadata["captured"] is True
    assert event.metadata["captured_email_id"] == capture.id


@override_settings(
    DATAMAILER_DELIVERY_MODE="capture",
    DATAMAILER_CAPTURE_UI=True,
    PUBLIC_BASE_URL="https://mail.example.com",
)
def test_campaign_worker_captures_rendered_recipient_without_ses(
    api_client_record,
    audience,
):
    campaign = Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        subject="Course starts",
        html_body='<p>Hello <a href="https://example.com">read</a></p>',
        text_body="Hello text",
    )
    contact = Contact.objects.create(email="learner@example.com")
    recipient = CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.PENDING,
    )

    result = send_campaign_batch(
        {
            "campaign_id": campaign.id,
            "campaign_recipient_ids": [recipient.id],
        },
        ses_client=_FailingIfCalledSes(),
    )

    recipient.refresh_from_db()
    capture = CapturedEmail.objects.get(campaign_recipient=recipient)
    assert result.sent_count == 1
    assert recipient.status == CampaignRecipientStatus.SENT
    assert recipient.ses_message_id == f"capture:{capture.id}"
    assert capture.source == "campaign"
    assert capture.event == "campaign"
    assert "https://mail.example.com/t/c/" in capture.html_body
    assert "https://mail.example.com/unsubscribe/" in capture.text_body


@override_settings(DATAMAILER_CAPTURE_UI=True)
def test_capture_api_lists_details_and_clears_runs(client, api_client_record):
    capture = CapturedEmail.objects.create(
        client=api_client_record,
        email="learner@example.com",
        from_email="courses@dtcdev.click",
        subject="Captured",
        html_body="<p>Captured</p>",
        text_body="Captured",
        source="transactional",
        event="homework_submission",
        idempotency_key="homework:42",
        metadata={"course_slug": "ml-zoomcamp-2026"},
    )

    list_response = client.get(
        reverse("mailing:api_testbed_runs"),
        {"source": "transactional", "event": "homework_submission"},
        **auth_headers(),
    )

    assert list_response.status_code == 200
    assert list_response.json()["runs"][0]["id"] == capture.id

    detail_response = client.get(
        reverse("mailing:api_testbed_run_detail", args=[capture.id]),
        **auth_headers(),
    )

    assert detail_response.status_code == 200
    assert detail_response.json()["run"]["html_body"] == "<p>Captured</p>"
    assert detail_response.json()["run"]["metadata"] == {
        "course_slug": "ml-zoomcamp-2026",
    }

    message_response = client.get(
        reverse("mailing:api_testbed_run_message", args=[capture.id, capture.id]),
        **auth_headers(),
    )

    assert message_response.status_code == 200
    assert message_response.json()["message"]["id"] == capture.id

    clear_response = client.delete(
        reverse("mailing:api_testbed_runs"),
        data={"email": "learner@example.com"},
        content_type="application/json",
        **auth_headers(),
    )

    assert clear_response.status_code == 200
    assert clear_response.json()["deleted_count"] == 1
    assert CapturedEmail.objects.count() == 0


@override_settings(DATAMAILER_CAPTURE_UI=False, DATAMAILER_DELIVERY_MODE="send")
def test_capture_api_can_be_disabled(client, api_client_record):
    response = client.get(reverse("mailing:api_testbed_runs"), **auth_headers())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "capture_api_disabled"


def _event(message_id, payload):
    return records_from_messages(
        [
            {
                "MessageId": message_id,
                "ReceiptHandle": f"{message_id}-receipt",
                "Body": json.dumps(payload),
            }
        ]
    )


class _FailingIfCalledSes:
    def send_email(self, **params):
        raise AssertionError("SES should not be called")
