"""Tests for the real inbox (SES inbound) read path.

The real inbox proves an email was actually sent via SES and received in a real
mailbox. These tests cover:

* :func:`is_real_inbox_address` recognition;
* that the transactional sender keeps a real-inbox address on the SES send path even
  when it would otherwise match the mock inbox;
* the S3 read helpers (list/get/clear), driven by an in-memory fake S3 client so no
  AWS calls are made;
* the read API views, including the disabled/auth/validation paths.
"""

import datetime as dt
from email.message import EmailMessage
from urllib.parse import urlencode

import pytest
from django.test import override_settings
from django.urls import reverse

from mailing.models import (
    Client,
    Contact,
    EmailTemplate,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.api import ApiValidationError
from mailing.services.auth import create_client_api_key
from mailing.services.real_inbox import (
    clear_received_messages,
    get_received_message,
    is_real_inbox_address,
    list_received_messages,
)
from mailing.services.transactional import build_transactional_queue_payload
from mailing.services.transactional_sender import send_transactional_email_from_queue

pytestmark = pytest.mark.django_db(transaction=True)

API_KEY = "real-inbox-key"
BUCKET = "datamailer-test-inbound-mail"

REAL_SETTINGS = {
    "REAL_INBOX_ENABLED": True,
    "REAL_INBOX_DOMAIN": "mailer.dtcdev.click",
    "REAL_INBOX_S3_BUCKET": BUCKET,
    "REAL_INBOX_S3_PREFIX": "raw/",
    "REAL_INBOX_MAX_SCAN_OBJECTS": 200,
}


# --- fake S3 ---------------------------------------------------------------


class _FakeBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _NoSuchKey(Exception):
    pass


class FakeS3Client:
    """Minimal in-memory S3 stand-in supporting list_objects_v2/get/delete."""

    def __init__(self):
        self.objects = {}  # key -> (bytes, last_modified)

        class _Exceptions:
            NoSuchKey = _NoSuchKey

        self.exceptions = _Exceptions()

    def put(self, key, raw, *, last_modified=None):
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        when = last_modified or dt.datetime.now(dt.timezone.utc)
        self.objects[key] = (raw, when)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class _Paginator:
            def paginate(self, Bucket, Prefix=""):
                contents = [
                    {"Key": key, "LastModified": lm}
                    for key, (_, lm) in outer.objects.items()
                    if key.startswith(Prefix)
                ]
                yield {"Contents": contents}

        return _Paginator()

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _NoSuchKey(Key)
        return {"Body": _FakeBody(self.objects[Key][0])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)
        return {}


def raw_mime(*, to, subject, text="hello", html=None, from_email="no-reply@dtcdev.click", date=None):
    message = EmailMessage()
    message["From"] = from_email
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = f"<{subject}@email.amazonses.com>"
    if date:
        message["Date"] = date
    if html:
        message.set_content(text)
        message.add_alternative(html, subtype="html")
    else:
        message.set_content(text)
    return message.as_bytes()


# --- fixtures --------------------------------------------------------------


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
        sender_emails=[{"id": "newsletter", "email": "newsletter@dtcdev.click"}],
        default_from_email="no-reply@dtcdev.click",
    )
    create_client_api_key(client=client, name="Real inbox test", raw_api_key=API_KEY)
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


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


# --- address recognition ---------------------------------------------------


@override_settings(**REAL_SETTINGS)
@pytest.mark.parametrize(
    "email,expected",
    [
        ("datamailer@mailer.dtcdev.click", True),
        ("datamailer+e2e-smoke-1@mailer.dtcdev.click", True),
        ("e2e+anything@mailer.dtcdev.click", True),
        ("E2E+Tag@Mailer.DTCdev.Click", True),
        ("someone@example.com", False),
        ("someone@mailbox.test", False),
        ("not-an-email", False),
    ],
)
def test_is_real_inbox_address(email, expected):
    assert is_real_inbox_address(email) is expected


def test_is_real_inbox_address_requires_domain_setting():
    with override_settings(REAL_INBOX_DOMAIN=""):
        assert is_real_inbox_address("datamailer@mailer.dtcdev.click") is False


# --- sender keeps real-inbox address on SES path ---------------------------


@override_settings(
    MOCK_INBOX_ENABLED=True,
    MOCK_INBOX_DOMAIN="mailer.dtcdev.click",  # would normally trigger mock capture
    REAL_INBOX_DOMAIN="mailer.dtcdev.click",  # but real-inbox takes precedence
    AWS_SES_CONFIGURATION_SET="",
)
def test_real_inbox_address_takes_real_ses_path(template, monkeypatch):
    """Even when an address also matches the mock inbox, real-inbox wins and SES is called."""
    email = "datamailer+e2e-smoke-1@mailer.dtcdev.click"
    contact, _ = Contact.objects.get_or_create(
        normalized_email=email, defaults={"email": email}
    )
    message = TransactionalMessage.objects.create(
        client=template.client,
        contact=contact,
        email=email,
        from_email_id="newsletter",
        from_email="no-reply@dtcdev.click",
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.QUEUED,
        idempotency_key="real-1",
        subject="Submission received",
        html_body="<p>Thanks</p>",
        text_body="Thanks",
    )
    payload = build_transactional_queue_payload(message)

    sent = {}

    def fake_send_email(*, ses_client, source, to_email, subject, html_body, text_body=""):
        sent["to"] = to_email
        sent["source"] = source
        return "ses-real-123"

    monkeypatch.setattr("mailing.services.transactional_sender.send_email", fake_send_email)

    result = send_transactional_email_from_queue(payload, client=object())

    assert sent["to"] == email  # SES really called, not mocked
    result.refresh_from_db()
    assert result.status == TransactionalMessageStatus.SENT
    assert result.ses_message_id == "ses-real-123"
    assert not result.ses_message_id.startswith("mock-inbox:")


# --- read helpers ----------------------------------------------------------


@override_settings(**REAL_SETTINGS)
def test_list_received_messages_filters_by_recipient():
    s3 = FakeS3Client()
    address = "datamailer+e2e-smoke-1@mailer.dtcdev.click"
    s3.put(
        "raw/aaa",
        raw_mime(to=address, subject="Mine-1", date="Mon, 15 Jun 2026 10:00:00 +0000"),
        last_modified=dt.datetime(2026, 6, 15, 10, tzinfo=dt.timezone.utc),
    )
    s3.put(
        "raw/bbb",
        raw_mime(to=address, subject="Mine-2", date="Mon, 15 Jun 2026 11:00:00 +0000"),
        last_modified=dt.datetime(2026, 6, 15, 11, tzinfo=dt.timezone.utc),
    )
    s3.put(
        "raw/ccc",
        raw_mime(to="datamailer+other@mailer.dtcdev.click", subject="NotMine"),
        last_modified=dt.datetime(2026, 6, 15, 12, tzinfo=dt.timezone.utc),
    )

    result = list_received_messages({"address": address}, client=s3)

    assert result["address"] == address
    assert result["count"] == 2
    subjects = [m["subject"] for m in result["messages"]]
    # newest first by LastModified
    assert subjects == ["Mine-2", "Mine-1"]
    assert all(address in m["to"] for m in result["messages"])


@override_settings(**REAL_SETTINGS)
def test_list_respects_limit():
    s3 = FakeS3Client()
    address = "e2e+t@mailer.dtcdev.click"
    for i in range(5):
        s3.put(
            f"raw/{i}",
            raw_mime(to=address, subject=f"S{i}"),
            last_modified=dt.datetime(2026, 6, 15, 10, i, tzinfo=dt.timezone.utc),
        )
    result = list_received_messages({"address": address, "limit": 2}, client=s3)
    assert result["count"] == 2


@override_settings(**REAL_SETTINGS)
def test_list_rejects_non_real_address():

    with pytest.raises(ApiValidationError) as exc:
        list_received_messages({"address": "student@example.com"}, client=FakeS3Client())
    assert exc.value.status_code == 422


@override_settings(**REAL_SETTINGS)
def test_get_received_message_returns_bodies():
    s3 = FakeS3Client()
    address = "e2e+t@mailer.dtcdev.click"
    s3.put(
        "raw/key1",
        raw_mime(to=address, subject="With bodies", text="plain text", html="<p>html body</p>"),
    )
    result = get_received_message({"address": address}, "raw/key1", client=s3)
    message = result["message"]
    assert message["subject"] == "With bodies"
    assert "plain text" in message["text_body"]
    assert "html body" in message["html_body"]
    assert address in message["to"]


@override_settings(**REAL_SETTINGS)
def test_get_received_message_404_for_wrong_address():

    s3 = FakeS3Client()
    s3.put("raw/key1", raw_mime(to="e2e+other@mailer.dtcdev.click", subject="x"))
    with pytest.raises(ApiValidationError) as exc:
        get_received_message({"address": "e2e+mine@mailer.dtcdev.click"}, "raw/key1", client=s3)
    assert exc.value.status_code == 404


@override_settings(**REAL_SETTINGS)
def test_clear_received_messages_deletes_only_address():
    s3 = FakeS3Client()
    mine = "e2e+mine@mailer.dtcdev.click"
    s3.put("raw/a", raw_mime(to=mine, subject="a"))
    s3.put("raw/b", raw_mime(to=mine, subject="b"))
    s3.put("raw/c", raw_mime(to="e2e+other@mailer.dtcdev.click", subject="c"))

    result = clear_received_messages({"address": mine}, client=s3)

    assert result["deleted_count"] == 2
    assert set(s3.objects) == {"raw/c"}


@override_settings(REAL_INBOX_ENABLED=True, REAL_INBOX_DOMAIN="mailer.dtcdev.click", REAL_INBOX_S3_BUCKET="")
def test_missing_bucket_raises_503():

    with pytest.raises(ApiValidationError) as exc:
        list_received_messages({"address": "e2e+t@mailer.dtcdev.click"}, client=FakeS3Client())
    assert exc.value.status_code == 503


# --- API views -------------------------------------------------------------


@override_settings(REAL_INBOX_ENABLED=False)
def test_api_disabled_returns_404(client):
    response = client.get(reverse("mailing:api_real_inbox_messages"), **auth_headers())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "real_inbox_disabled"


@override_settings(**REAL_SETTINGS)
def test_api_requires_auth(client):
    response = client.get(
        reverse("mailing:api_real_inbox_messages"),
        {"address": "e2e+t@mailer.dtcdev.click"},
    )
    assert response.status_code == 401


@override_settings(**REAL_SETTINGS)
def test_api_list_messages(client, api_client_record, monkeypatch):
    s3 = FakeS3Client()
    address = "datamailer+e2e-smoke-9@mailer.dtcdev.click"
    s3.put(
        "raw/x",
        raw_mime(to=address, subject="Submission received", html="<p>link</p>"),
        last_modified=dt.datetime(2026, 6, 15, 10, tzinfo=dt.timezone.utc),
    )
    monkeypatch.setattr("mailing.services.real_inbox.s3_client", lambda **kw: s3)

    response = client.get(
        reverse("mailing:api_real_inbox_messages"),
        {"address": address},
        **auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["messages"][0]["subject"] == "Submission received"


@override_settings(**REAL_SETTINGS)
def test_api_clear_messages(client, api_client_record, monkeypatch):
    s3 = FakeS3Client()
    address = "datamailer+e2e-smoke-9@mailer.dtcdev.click"
    s3.put("raw/x", raw_mime(to=address, subject="s"))
    monkeypatch.setattr("mailing.services.real_inbox.s3_client", lambda **kw: s3)

    response = client.delete(
        reverse("mailing:api_real_inbox_messages") + "?" + urlencode({"address": address}),
        **auth_headers(),
    )
    assert response.status_code == 200
    assert response.json()["deleted_count"] == 1
    assert s3.objects == {}
