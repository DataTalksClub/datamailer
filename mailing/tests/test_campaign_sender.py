import json
import re

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from django.test import override_settings

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    Organization,
)
from mailing.services.campaign_sender import build_campaign_html_body, send_campaign_batch
from mailing.services.tokens import token_hash
from mailing.sqs import records_from_messages
from mailing.workers import campaign_email_handler

pytestmark = pytest.mark.django_db


@pytest.fixture
def campaign_setup():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    audience = Audience.objects.create(organization=organization, name="Newsletter", slug="newsletter")
    client = Client.objects.create(organization=organization, name="Datamailer", slug="datamailer")
    campaign = Campaign.objects.create(
        audience=audience,
        client=client,
        subject="Weekly update",
        html_body='<p>Hello <a href="https://example.com/post?a=1&b=2">read</a> <a href="mailto:a@example.com">mail</a></p>',
        text_body="Hello in text",
    )
    contact = Contact.objects.create(email="person@example.com")
    recipient = CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email)
    return campaign, recipient


@override_settings(
    AWS_REGION="us-east-1",
    AWS_SES_CONFIGURATION_SET="campaign-events",
    DEFAULT_FROM_EMAIL="newsletter@example.com",
    PUBLIC_BASE_URL="https://mail.example.com",
)
def test_campaign_sender_sends_pending_recipient_and_persists_audit_state(campaign_setup):
    campaign, recipient = campaign_setup
    ses = _ses_client()

    with Stubber(ses) as stubber:
        stubber.add_response(
            "send_email",
            {"MessageId": "ses-message-1"},
            {
                "Source": "newsletter@example.com",
                "Destination": {"ToAddresses": ["person@example.com"]},
                "Message": {
                    "Subject": {"Charset": "UTF-8", "Data": "Weekly update"},
                    "Body": {
                        "Html": {
                            "Charset": "UTF-8",
                            "Data": _html_body_matcher(
                                [
                                    'https://mail.example.com/t/o/',
                                    'https://mail.example.com/t/c/',
                                    "u=https%3A%2F%2Fexample.com%2Fpost%3Fa%3D1%26b%3D2",
                                    'href="mailto:a@example.com"',
                                    "Unsubscribe or manage preferences",
                                ]
                            ),
                        },
                        "Text": {
                            "Charset": "UTF-8",
                            "Data": _text_body_matcher(
                                [
                                    "Hello in text",
                                    "Unsubscribe or manage preferences: https://mail.example.com/unsubscribe/",
                                ]
                            ),
                        },
                    },
                },
                "ConfigurationSetName": "campaign-events",
            },
        )

        result = send_campaign_batch(_payload(campaign, recipient), ses_client=ses)

    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert result.sent_count == 1
    assert recipient.status == CampaignRecipientStatus.SENT
    assert recipient.ses_message_id == "ses-message-1"
    assert recipient.sent_at is not None
    assert recipient.last_error == ""
    assert campaign.sent_count == 1
    event = EmailEvent.objects.get(campaign_recipient=recipient)
    assert event.event_type == EmailEventType.SENT
    assert event.metadata == {"ses_message_id": "ses-message-1"}


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_handler_is_idempotent_for_duplicate_sqs_delivery(campaign_setup, monkeypatch):
    campaign, recipient = campaign_setup
    sends = []

    class FakeSes:
        def send_email(self, **params):
            sends.append(params)
            return {"MessageId": "ses-message-1"}

    monkeypatch.setattr("mailing.services.campaign_sender.default_ses_client", lambda: FakeSes())

    event = _event_from_payloads([("message-1", _payload(campaign, recipient))])

    assert campaign_email_handler(event, None) == {"batchItemFailures": []}
    assert campaign_email_handler(event, None) == {"batchItemFailures": []}

    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert len(sends) == 1
    assert recipient.status == CampaignRecipientStatus.SENT
    assert campaign.sent_count == 1
    assert EmailEvent.objects.filter(campaign_recipient=recipient, event_type=EmailEventType.SENT).count() == 1


@pytest.mark.parametrize(
    "status",
    [
        CampaignRecipientStatus.SENT,
        CampaignRecipientStatus.SKIPPED,
        CampaignRecipientStatus.FAILED,
        CampaignRecipientStatus.BOUNCED,
        CampaignRecipientStatus.COMPLAINED,
        CampaignRecipientStatus.UNSUBSCRIBED,
    ],
)
def test_campaign_sender_acknowledges_non_pending_terminal_recipients_without_ses(campaign_setup, status):
    campaign, recipient = campaign_setup
    recipient.status = status
    recipient.ses_message_id = "already-sent" if status == CampaignRecipientStatus.SENT else ""
    recipient.save()

    result = send_campaign_batch(_payload(campaign, recipient), ses_client=_FailingIfCalledSes())

    assert result.skipped_count == 1
    assert EmailEvent.objects.count() == 0


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_sender_records_permanent_failure_and_acknowledges_record(campaign_setup, monkeypatch):
    campaign, recipient = campaign_setup

    class RejectingSes:
        def send_email(self, **params):
            raise ClientError({"Error": {"Code": "MessageRejected", "Message": "Address rejected"}}, "SendEmail")

    monkeypatch.setattr("mailing.services.campaign_sender.default_ses_client", lambda: RejectingSes())
    event = _event_from_payloads([("message-1", _payload(campaign, recipient))])

    assert campaign_email_handler(event, None) == {"batchItemFailures": []}

    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.FAILED
    assert "MessageRejected" in recipient.last_error
    assert campaign.sent_count == 0
    assert EmailEvent.objects.get(campaign_recipient=recipient).event_type == EmailEventType.FAILED


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_handler_returns_partial_batch_failure_for_retryable_send(campaign_setup, monkeypatch):
    campaign, recipient = campaign_setup
    second_contact = Contact.objects.create(email="second@example.com")
    second_recipient = CampaignRecipient.objects.create(campaign=campaign, contact=second_contact, email=second_contact.email)

    class MixedSes:
        def send_email(self, **params):
            if params["Destination"]["ToAddresses"] == ["person@example.com"]:
                raise ClientError(
                    {
                        "Error": {"Code": "Throttling", "Message": "Rate exceeded"},
                        "ResponseMetadata": {"HTTPStatusCode": 429},
                    },
                    "SendEmail",
                )
            return {"MessageId": "ses-message-2"}

    monkeypatch.setattr("mailing.services.campaign_sender.default_ses_client", lambda: MixedSes())
    event = _event_from_payloads(
        [
            ("retry-message", _payload(campaign, recipient)),
            ("ok-message", _payload(campaign, second_recipient)),
        ]
    )

    assert campaign_email_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "retry-message"}],
    }

    recipient.refresh_from_db()
    second_recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.PENDING
    assert "Throttling" in recipient.last_error
    assert second_recipient.status == CampaignRecipientStatus.SENT
    assert campaign.sent_count == 1


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_handler_refreshes_sent_count_when_single_record_partially_sends_before_retry(campaign_setup, monkeypatch):
    campaign, recipient = campaign_setup
    second_contact = Contact.objects.create(email="second@example.com")
    second_recipient = CampaignRecipient.objects.create(campaign=campaign, contact=second_contact, email=second_contact.email)

    class MixedSes:
        def send_email(self, **params):
            if params["Destination"]["ToAddresses"] == ["second@example.com"]:
                raise ClientError(
                    {
                        "Error": {"Code": "Throttling", "Message": "Rate exceeded"},
                        "ResponseMetadata": {"HTTPStatusCode": 429},
                    },
                    "SendEmail",
                )
            return {"MessageId": "ses-message-1"}

    monkeypatch.setattr("mailing.services.campaign_sender.default_ses_client", lambda: MixedSes())
    payload = _payload(campaign, recipient) | {"campaign_recipient_ids": [recipient.id, second_recipient.id]}
    event = _event_from_payloads([("mixed-message", payload)])

    assert campaign_email_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "mixed-message"}],
    }

    recipient.refresh_from_db()
    second_recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert recipient.status == CampaignRecipientStatus.SENT
    assert recipient.ses_message_id == "ses-message-1"
    assert second_recipient.status == CampaignRecipientStatus.PENDING
    assert "Throttling" in second_recipient.last_error
    assert campaign.sent_count == 1


@override_settings(DEFAULT_FROM_EMAIL="newsletter@example.com", PUBLIC_BASE_URL="https://mail.example.com")
def test_campaign_handler_can_retry_pending_recipient_with_token_hashes_after_transient_failure(
    campaign_setup,
    monkeypatch,
):
    campaign, recipient = campaign_setup
    sends = []

    class FlakySes:
        def send_email(self, **params):
            sends.append(params)
            if len(sends) == 1:
                raise ClientError(
                    {
                        "Error": {"Code": "Throttling", "Message": "Rate exceeded"},
                        "ResponseMetadata": {"HTTPStatusCode": 429},
                    },
                    "SendEmail",
                )
            return {"MessageId": "ses-message-2"}

    monkeypatch.setattr("mailing.services.campaign_sender.default_ses_client", lambda: FlakySes())
    event = _event_from_payloads([("retry-message", _payload(campaign, recipient))])

    assert campaign_email_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "retry-message"}],
    }

    recipient.refresh_from_db()
    recipient.tracking_token_hash = recipient.tracking_token_hash or token_hash("stale-tracking-token")
    recipient.unsubscribe_token_hash = recipient.unsubscribe_token_hash or token_hash("stale-unsubscribe-token")
    recipient.save(update_fields=["tracking_token_hash", "unsubscribe_token_hash", "updated_at"])

    assert campaign_email_handler(event, None) == {"batchItemFailures": []}

    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert len(sends) == 2
    assert recipient.status == CampaignRecipientStatus.SENT
    assert recipient.ses_message_id == "ses-message-2"
    assert campaign.sent_count == 1
    assert EmailEvent.objects.filter(campaign_recipient=recipient, event_type=EmailEventType.SENT).count() == 1

    html_body = sends[1]["Message"]["Body"]["Html"]["Data"]
    text_body = sends[1]["Message"]["Body"]["Text"]["Data"]
    tracking_token = re.search(r"/t/o/([A-Za-z0-9_-]+)\.gif", html_body).group(1)
    unsubscribe_token = re.search(r"/unsubscribe/([A-Za-z0-9_-]+)", html_body).group(1)
    assert token_hash(tracking_token) == recipient.tracking_token_hash
    assert token_hash(unsubscribe_token) == recipient.unsubscribe_token_hash
    assert f"https://mail.example.com/t/c/{tracking_token}?u=" in html_body
    assert f"https://mail.example.com/unsubscribe/{unsubscribe_token}" in text_body


def test_campaign_sender_rejects_recipient_ids_from_another_campaign(campaign_setup):
    campaign, recipient = campaign_setup
    other_campaign = Campaign.objects.create(
        audience=campaign.audience,
        client=campaign.client,
        subject="Other",
    )
    other_recipient = CampaignRecipient.objects.create(campaign=other_campaign, contact=recipient.contact, email=recipient.email)

    event = _event_from_payloads([("bad-message", _payload(campaign, other_recipient))])

    assert campaign_email_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "bad-message"}],
    }


@override_settings(PUBLIC_BASE_URL="https://mail.example.com")
def test_build_campaign_html_body_adds_tracking_pixel_click_links_and_unsubscribe_link():
    html = build_campaign_html_body(
        '<a href="https://example.com/path">tracked</a><a href="/relative">relative</a>',
        "tracking-token",
        "unsubscribe-token",
    )

    assert 'src="https://mail.example.com/t/o/tracking-token.gif"' in html
    assert 'href="https://mail.example.com/t/c/tracking-token?u=https%3A%2F%2Fexample.com%2Fpath"' in html
    assert 'href="/relative"' in html
    assert 'href="https://mail.example.com/unsubscribe/unsubscribe-token"' in html


def _payload(campaign, recipient):
    return {
        "contract": "campaign-email",
        "version": 1,
        "campaign_id": campaign.id,
        "batch_id": f"campaign-{campaign.id}-batch-1",
        "campaign_recipient_ids": [recipient.id],
    }


def _event_from_payloads(message_payloads):
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


def _ses_client():
    return boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


class _FailingIfCalledSes:
    def send_email(self, **params):
        raise AssertionError("SES should not be called")


class _html_body_matcher:
    def __init__(self, expected_parts):
        self.expected_parts = expected_parts

    def __eq__(self, other):
        return all(part in other for part in self.expected_parts)


class _text_body_matcher:
    def __init__(self, expected_parts):
        self.expected_parts = expected_parts

    def __eq__(self, other):
        return all(part in other for part in self.expected_parts)
