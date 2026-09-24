import io
import json
from email.message import EmailMessage
from types import SimpleNamespace

import pytest
from django.test import override_settings

from mailing.inbound_mime import address_values, parse_mime
from mailing.services.inbound_email import matching_routes, process_inbound_s3_notification


class ConditionalCheckFailedException(Exception):
    pass


class FakeDynamoDB:
    exceptions = SimpleNamespace(ConditionalCheckFailedException=ConditionalCheckFailedException)

    def __init__(self):
        self.items = {}

    def put_item(self, TableName, Item, ConditionExpression):
        key = Item["event_id"]["S"]
        if key in self.items:
            raise ConditionalCheckFailedException
        self.items[key] = Item

    def delete_item(self, TableName, Key):
        self.items.pop(Key["event_id"]["S"], None)


class FakeS3:
    def __init__(self, raw):
        self.raw = raw
        self.puts = []

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.raw)}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)


class FakePublisher:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    def publish(self, **kwargs):
        if self.fail:
            raise RuntimeError("publish failed")
        self.messages.append(kwargs)


def raw_message(*, message_id="<mail-123@example.com>"):
    message = EmailMessage()
    message["From"] = "Billing <billing@example.com>"
    message["To"] = "Invoice <invoice@mailer.test>, TODO <todo@mailer.test>"
    message["Cc"] = "Copy <copy@example.com>"
    message["Subject"] = "Receipt for July"
    message["Date"] = "Sun, 12 Jul 2026 09:30:00 +0200"
    if message_id is not None:
        message["Message-ID"] = message_id
    message.set_content("Plain receipt body")
    message.add_alternative("<html><body><strong>HTML receipt body</strong></body></html>", subtype="html")
    message.add_attachment(b"pdf bytes", maintype="application", subtype="pdf", filename="receipt July.pdf")
    return message.as_bytes()


def s3_event():
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "s3": {
                    "bucket": {"name": "inbound-private"},
                    "object": {"key": "raw%2Fmessage-123"},
                },
            }
        ]
    }


def test_parser_extracts_alternatives_addresses_and_attachment():
    parsed = parse_mime(raw_message())

    assert parsed.sender_addresses == ["billing@example.com"]
    assert parsed.recipient_addresses == ["invoice@mailer.test", "todo@mailer.test", "copy@example.com"]
    assert parsed.body_text.strip() == "Plain receipt body"
    assert "HTML receipt body" in parsed.body_html
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].filename == "receipt July.pdf"
    assert parsed.attachments[0].payload == b"pdf bytes"


@override_settings(
    INBOUND_EMAIL_ROUTES={"invoice@mailer.test": "invoice", "todo@mailer.test": "todo"},
    INBOUND_EMAIL_IDEMPOTENCY_TABLE="inbound-idempotency",
    INBOUND_EMAIL_EVENTS_TOPIC_ARN="arn:aws:sns:eu-west-1:123:inbound-events",
    INBOUND_EMAIL_ARTIFACT_PREFIX="processed/",
    INBOUND_EMAIL_INLINE_BODY_MAX_BYTES=65536,
)
def test_worker_routes_multiple_aliases_persists_attachment_and_is_idempotent():
    s3 = FakeS3(raw_message())
    dynamodb = FakeDynamoDB()
    publisher = FakePublisher()

    first = process_inbound_s3_notification(s3_event(), s3=s3, dynamodb=dynamodb, publisher=publisher)
    second = process_inbound_s3_notification(s3_event(), s3=s3, dynamodb=dynamodb, publisher=publisher)

    assert [item["route"] for item in first] == ["invoice", "todo"]
    assert all(item["idempotent_replay"] is False for item in first)
    assert all(item["idempotent_replay"] is True for item in second)
    assert len(publisher.messages) == 2
    events = [json.loads(item["Message"]) for item in publisher.messages]
    assert {event["route"] for event in events} == {"invoice", "todo"}
    assert all(event["contract"] == "inbound-email" and event["version"] == 1 for event in events)
    assert all(event["raw_mime"] == {"bucket": "inbound-private", "key": "raw/message-123"} for event in events)
    assert all(event["attachments"][0]["s3"]["bucket"] == "inbound-private" for event in events)
    assert len(s3.puts) == 2


@override_settings(
    INBOUND_EMAIL_ROUTES={"invoice@mailer.test": "invoice"},
    INBOUND_EMAIL_IDEMPOTENCY_TABLE="inbound-idempotency",
    INBOUND_EMAIL_EVENTS_TOPIC_ARN="arn:aws:sns:eu-west-1:123:inbound-events",
    INBOUND_EMAIL_ARTIFACT_PREFIX="processed/",
    INBOUND_EMAIL_INLINE_BODY_MAX_BYTES=4,
)
def test_worker_stores_large_bodies_and_releases_claim_after_publish_failure():
    s3 = FakeS3(raw_message())
    dynamodb = FakeDynamoDB()

    with pytest.raises(RuntimeError, match="publish failed"):
        process_inbound_s3_notification(s3_event(), s3=s3, dynamodb=dynamodb, publisher=FakePublisher(fail=True))

    assert dynamodb.items == {}
    publisher = FakePublisher()
    process_inbound_s3_notification(s3_event(), s3=s3, dynamodb=dynamodb, publisher=publisher)
    event = json.loads(publisher.messages[0]["Message"])
    assert event["body"]["text"]["s3"]["key"].endswith("/body.txt")
    assert event["body"]["html"]["s3"]["key"].endswith("/body.html")


@override_settings(
    INBOUND_EMAIL_ROUTES={"invoice@mailer.test": "invoice"},
    INBOUND_EMAIL_IDEMPOTENCY_TABLE="inbound-idempotency",
    INBOUND_EMAIL_EVENTS_TOPIC_ARN="arn:aws:sns:eu-west-1:123:inbound-events",
)
def test_worker_rejects_message_without_message_id():
    with pytest.raises(ValueError, match="missing Message-ID"):
        process_inbound_s3_notification(
            s3_event(), s3=FakeS3(raw_message(message_id=None)), dynamodb=FakeDynamoDB(), publisher=FakePublisher()
        )


@pytest.mark.parametrize("raw,error", [(b"", "empty MIME"), (b"not a MIME message", "no headers")])
def test_parser_rejects_malformed_mime(raw, error):
    with pytest.raises(ValueError, match=error):
        parse_mime(raw)


def test_address_values_ignores_empty_optional_headers():
    assert address_values("invoice@example.com", "", None) == ["invoice@example.com"]


@override_settings(INBOUND_EMAIL_ROUTE_DOMAINS={"dtcdev.click"})
def test_matching_routes_falls_back_to_local_part_on_trigger_domains():
    routes = matching_routes(
        ["invoice-2026@dtcdev.click", "keep@mailer.test", "Bad_Name@dtcdev.click", "x@other.test"],
        {"keep@mailer.test": "keep"},
    )

    assert routes == {
        "invoice-2026": ["invoice-2026@dtcdev.click"],
        "keep": ["keep@mailer.test"],
    }


def test_matching_routes_exact_entry_wins_over_fallback():
    routes = matching_routes(["special@dtcdev.click"], {"special@dtcdev.click": "renamed"})

    assert routes == {"renamed": ["special@dtcdev.click"]}


@override_settings(
    INBOUND_EMAIL_ROUTES={},
    INBOUND_EMAIL_ROUTE_DOMAINS={"dtcdev.click"},
    INBOUND_EMAIL_IDEMPOTENCY_TABLE="inbound-idempotency",
    INBOUND_EMAIL_EVENTS_TOPIC_ARN="arn:aws:sns:eu-west-1:123:inbound-events",
    INBOUND_EMAIL_ARTIFACT_PREFIX="processed/",
    INBOUND_EMAIL_INLINE_BODY_MAX_BYTES=65536,
)
def test_worker_publishes_trigger_domain_address_as_its_own_route():
    message = EmailMessage()
    message["From"] = "Billing <billing@example.com>"
    message["To"] = "income-2026-08@dtcdev.click"
    message["Message-ID"] = "<trigger-123@example.com>"
    message.set_content("Please file this invoice")
    s3 = FakeS3(message.as_bytes())
    dynamodb = FakeDynamoDB()
    publisher = FakePublisher()

    results = process_inbound_s3_notification(
        {
            "Records": [
                {
                    "eventSource": "aws:s3",
                    "s3": {
                        "bucket": {"name": "inbound-private"},
                        "object": {"key": "raw%2Fmessage-trigger"},
                    },
                }
            ]
        },
        s3=s3,
        dynamodb=dynamodb,
        publisher=publisher,
    )

    assert [item["route"] for item in results] == ["income-2026-08"]
    event = json.loads(publisher.messages[0]["Message"])
    assert event["route"] == "income-2026-08"
