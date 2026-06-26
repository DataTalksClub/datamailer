import boto3
import pytest
from botocore.stub import Stubber
from django.test import override_settings

import mailing.ses as ses_module
from mailing.ses import send_email


@override_settings(AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_send_email_uses_expected_ses_payload():
    client = boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "send_email",
            {"MessageId": "message-123"},
            {
                "Source": "newsletter@example.com",
                "Destination": {"ToAddresses": ["person@example.com"]},
                "Message": {
                    "Subject": {"Charset": "UTF-8", "Data": "Welcome"},
                    "Body": {
                        "Html": {"Charset": "UTF-8", "Data": "<p>Hello</p>"},
                        "Text": {"Charset": "UTF-8", "Data": "Hello"},
                    },
                },
            },
        )

        message_id = send_email(
            ses_client=client,
            source="newsletter@example.com",
            to_email="person@example.com",
            subject="Welcome",
            html_body="<p>Hello</p>",
            text_body="Hello",
        )

    assert message_id == "message-123"


@override_settings(AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="")
def test_send_email_uses_reply_to_address():
    client = boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        stubber.add_response(
            "send_email",
            {"MessageId": "message-123"},
            {
                "Source": "newsletter@example.com",
                "Destination": {"ToAddresses": ["person@example.com"]},
                "Message": {
                    "Subject": {"Charset": "UTF-8", "Data": "Welcome"},
                    "Body": {
                        "Html": {"Charset": "UTF-8", "Data": "<p>Hello</p>"},
                    },
                },
                "ReplyToAddresses": ["support@example.com"],
            },
        )

        message_id = send_email(
            ses_client=client,
            source="newsletter@example.com",
            to_email="person@example.com",
            subject="Welcome",
            html_body="<p>Hello</p>",
            reply_to="support@example.com",
        )

    assert message_id == "message-123"


@override_settings(AWS_REGION="us-east-1", AWS_SES_CONFIGURATION_SET="", SES_MAX_SEND_RATE_PER_SECOND=2)
def test_send_email_throttles_between_ses_calls(monkeypatch):
    ses_module._last_ses_send_monotonic = None
    monotonic_values = iter([10.0, 10.1])
    sleeps = []
    monkeypatch.setattr(ses_module.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(ses_module.time, "sleep", sleeps.append)

    client = boto3.client(
        "ses",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        expected = {
            "Source": "newsletter@example.com",
            "Destination": {"ToAddresses": ["person@example.com"]},
            "Message": {
                "Subject": {"Charset": "UTF-8", "Data": "Welcome"},
                "Body": {"Html": {"Charset": "UTF-8", "Data": "<p>Hello</p>"}},
            },
        }
        stubber.add_response("send_email", {"MessageId": "message-1"}, expected)
        stubber.add_response("send_email", {"MessageId": "message-2"}, expected)

        for _ in range(2):
            send_email(
                ses_client=client,
                source="newsletter@example.com",
                to_email="person@example.com",
                subject="Welcome",
                html_body="<p>Hello</p>",
            )

    assert sleeps == [pytest.approx(0.4)]
    ses_module._last_ses_send_monotonic = None
