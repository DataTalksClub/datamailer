import json

import pytest
from django.contrib import admin
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from mailing.admin import EmailEventAdmin, EmailTemplateAdmin, TransactionalMessageAdmin
from mailing.models import (
    Audience,
    CategoryPreference,
    Client,
    CmpCallback,
    CmpCallbackStatus,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    RecipientList,
    RecipientListMember,
    Subscription,
    SubscriptionStatus,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.queue_contracts import validate_transactional_email_message
from mailing.services.auth import create_client_api_key
from mailing.services.cmp_callbacks import process_due_cmp_callbacks

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
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def other_client(organization):
    client = Client.objects.create(
        organization=organization,
        name="DTC Newsletter",
        slug="dtc-newsletter",
        default_sender_id="newsletter",
        sender_emails=[{"id": "newsletter", "email": "newsletter@example.com"}],
    )
    create_client_api_key(client=client, name="Other test", raw_api_key="other-key")
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


def post_transactional(django_client, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_transactional_send"),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
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


def post_recipient_list_transactional(django_client, list_key, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_recipient_list_transactional_send", args=[list_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def post_transient_recipient_list_transactional(django_client, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_transient_recipient_list_transactional_send"),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def put_transactional_template(django_client, template_key, payload, raw_key=API_KEY):
    return django_client.put(
        reverse("mailing:api_transactional_template", args=[template_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def put_client_senders(django_client, payload, raw_key=API_KEY):
    return django_client.put(
        reverse("mailing:api_client_senders"),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def test_transactional_template_api_upserts_and_returns_client_template(
    client,
    api_client_record,
):
    payload = {
        "name": "Homework Submission Confirmation",
        "description": "Confirm that a homework submission was saved.",
        "subject": "Homework submission received: {{ homework_title }}",
        "html_body": "<p>{{ homework_title }} was saved.</p>",
        "text_body": "{{ homework_title }} was saved.",
        "required_context": [{"name": "homework_title", "description": "Homework title."}],
        "example_context": {"homework_title": "Homework 1"},
    }

    response = put_transactional_template(
        client,
        "homework-submission-confirmation",
        payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["template"]["key"] == "homework-submission-confirmation"
    assert body["template"]["client"] == api_client_record.slug
    assert body["template"]["subject"] == "Homework submission received: {{ homework_title }}"
    assert body["template"]["required_context"] == payload["required_context"]
    assert body["template"]["example_context"] == payload["example_context"]
    assert body["template"]["is_transactional"] is True
    assert body["template"]["is_active"] is True

    template = EmailTemplate.objects.get()
    assert template.client == api_client_record
    assert template.key == "homework-submission-confirmation"

    get_response = client.get(
        reverse("mailing:api_transactional_template", args=[template.key]),
        **auth_headers(),
    )

    assert get_response.status_code == 200
    assert get_response.json()["key"] == template.key
    assert get_response.json()["name"] == payload["name"]


def test_transactional_template_api_updates_existing_template(client, template):
    response = put_transactional_template(
        client,
        template.key,
        {
            "name": "Updated Email Verification",
            "subject": "Updated {{ product }}",
            "html_body": "<p>Updated</p>",
            "text_body": "Updated",
            "required_context": [{"name": "product"}],
            "example_context": {"product": "Datamailer"},
            "is_active": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["created"] is False

    template.refresh_from_db()
    assert template.name == "Updated Email Verification"
    assert template.subject == "Updated {{ product }}"
    assert template.is_active is False


def test_transactional_template_api_is_scoped_to_authenticated_client(
    client,
    template,
    other_client,
):
    response = client.get(
        reverse("mailing:api_transactional_template", args=[template.key]),
        **auth_headers("other-key"),
    )

    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"template_key": "not_found"}

    put_response = put_transactional_template(
        client,
        template.key,
        {
            "name": "Other Client Template",
            "subject": "Other",
            "text_body": "Other",
        },
        raw_key="other-key",
    )

    assert put_response.status_code == 200
    assert put_response.json()["created"] is True
    assert EmailTemplate.objects.filter(key=template.key).count() == 2


def test_transactional_template_api_validates_payload(client, api_client_record):
    response = put_transactional_template(
        client,
        "homework-submission-confirmation",
        {
            "name": "",
            "subject": "",
            "required_context": {},
            "example_context": [],
            "is_active": "yes",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {
        "name": "required",
        "subject": "required",
        "required_context": "must_be_list",
        "example_context": "must_be_object",
        "is_active": "must_be_boolean",
    }
    assert EmailTemplate.objects.count() == 0


def test_transactional_send_creates_message_event_and_contract_queue_payload(
    client,
    audience,
    api_client_record,
    template,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_transactional(
        client,
        {
            "email": " Person@Example.COM ",
            "audience": audience.slug,
            "client": api_client_record.slug,
            "template_key": template.key,
            "idempotency_key": "verify-123",
            "context": {
                "product": "Datamailer",
                "verification_url": "https://example.com/verify/token",
            },
            "category_tag": "course-updates",
            "reply_to": "support@example.com",
            "metadata": {"user_id": "42"},
        },
    )

    message = TransactionalMessage.objects.get()
    contact = Contact.objects.get()
    event = EmailEvent.objects.get()

    assert response.status_code == 202
    assert response.json()["message"]["id"] == message.id
    assert response.json()["message"]["from_email"] == "newsletter"
    assert response.json()["message"]["from_email_address"] == "newsletter@example.com"
    assert response.json()["message"]["reply_to"] == "support@example.com"
    assert response.json()["message"]["status"] == TransactionalMessageStatus.QUEUED
    assert response.json()["idempotent_replay"] is False
    assert response.json()["enqueued"] is True
    assert contact.normalized_email == "person@example.com"
    assert message.email == "person@example.com"
    assert message.from_email_id == "newsletter"
    assert message.from_email == "newsletter@example.com"
    assert message.template == template
    assert message.template_key == template.key
    assert message.subject == "Verify Datamailer"
    assert message.html_body == "<p>Verify at https://example.com/verify/token</p>"
    assert message.text_body == "Verify at https://example.com/verify/token"
    assert message.context["verification_url"] == "https://example.com/verify/token"
    assert message.metadata == {
        "user_id": "42",
        "category_tag": "course-updates",
        "reply_to": "support@example.com",
    }
    assert event.event_type == EmailEventType.QUEUED
    assert event.transactional_message == message
    assert len(enqueued) == 1
    assert validate_transactional_email_message(enqueued[0]) == enqueued[0]
    assert enqueued[0]["contract"] == "transactional-email"
    assert enqueued[0]["transactional_message_id"] == message.id
    assert enqueued[0]["metadata"]["reply_to"] == "support@example.com"
    assert enqueued[0]["client_id"] == template.client_id
    assert enqueued[0]["contact_id"] == contact.id
    assert enqueued[0]["template_id"] == template.id
    assert enqueued[0]["template_key"] == template.key
    assert enqueued[0]["idempotency_key"] == "verify-123"


def test_transactional_send_skips_category_opted_out_contact(
    client,
    audience,
    api_client_record,
    template,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    contact = Contact.objects.create(email="person@example.com")
    CategoryPreference.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        tag="course-updates",
        label="Course updates",
        enabled=False,
    )

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "audience": audience.slug,
            "client": api_client_record.slug,
            "template_key": template.key,
            "idempotency_key": "course-update-123",
            "context": {"product": "Datamailer"},
            "category_tag": "course-updates",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["reason"] == "category_unsubscribe"
    message = TransactionalMessage.objects.get()
    assert message.status == TransactionalMessageStatus.SKIPPED
    assert message.last_error == "category_unsubscribe"
    assert enqueued == []


@override_settings(CMP_WEBHOOK_URL="https://cmp.example.com/api/datamailer/events", CMP_WEBHOOK_TOKEN="secret")
def test_transactional_send_to_recipient_list_creates_per_member_messages(
    client,
    audience,
    api_client_record,
    template,
    monkeypatch,
):
    enqueued = []
    posts = collect_cmp_callbacks(monkeypatch)
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    allowed_contact = Contact.objects.create(email="allowed@example.com")
    suppressed_contact = Contact.objects.create(
        email="suppressed@example.com",
        hard_bounced_at=timezone.now(),
    )
    recipient_list = RecipientList.objects.create(
        client=api_client_record,
        audience=audience,
        key="ml-zoomcamp-2026:@e:@homework:homework-1",
        type="homework_submitters",
        name="Homework 1 submitters",
        member_count=2,
        active_member_count=2,
    )
    RecipientListMember.objects.create(
        recipient_list=recipient_list,
        contact=allowed_contact,
        email=allowed_contact.normalized_email,
        source_object_key="homework-submission:1",
    )
    RecipientListMember.objects.create(
        recipient_list=recipient_list,
        contact=suppressed_contact,
        email=suppressed_contact.normalized_email,
        source_object_key="homework-submission:2",
    )
    CategoryPreference.objects.create(
        contact=allowed_contact,
        audience=audience,
        client=api_client_record,
        tag="submission-results",
        label="Submission results",
        enabled=False,
    )

    payload = {
        "audience": audience.slug,
        "client": api_client_record.slug,
        "template_key": template.key,
        "idempotency_key": "homework-score:homework-1",
        "context": {
            "product": "ML Zoomcamp",
            "verification_url": "https://courses.example.com/scores",
        },
        "category_tag": "submission-results",
        "metadata": {"source": "score-publication"},
    }

    response = post_recipient_list_transactional(client, recipient_list.key, payload)

    assert response.status_code == 202
    body = response.json()
    assert body["created_count"] == 2
    assert body["enqueued_count"] == 0
    assert body["skipped_count"] == 2
    assert body["idempotent_replay_count"] == 0
    messages = list(TransactionalMessage.objects.order_by("email"))
    assert [message.email for message in messages] == ["allowed@example.com", "suppressed@example.com"]
    assert messages[0].idempotency_key == "homework-score:homework-1:homework-submission:1"
    assert messages[0].status == TransactionalMessageStatus.SKIPPED
    assert messages[0].metadata["recipient_list_key"] == recipient_list.key
    assert messages[0].metadata["category_tag"] == "submission-results"
    assert messages[0].last_error == "category_unsubscribe"
    assert messages[1].idempotency_key == "homework-score:homework-1:homework-submission:2"
    assert messages[1].status == TransactionalMessageStatus.SKIPPED
    assert messages[1].last_error == "hard_bounce"
    assert len(enqueued) == 0
    assert CmpCallback.objects.filter(status=CmpCallbackStatus.PENDING).count() == 2
    process_due_cmp_callbacks()
    assert len(posts) == 2
    reasons_by_email = {
        post["json"]["email"]: post["json"]["metadata"]["reason"]
        for post in posts
    }
    assert posts[0]["url"] == "https://cmp.example.com/api/datamailer/events"
    assert posts[0]["headers"]["Authorization"] == "Bearer secret"
    assert {post["json"]["event_type"] for post in posts} == {"transactional.skipped"}
    assert {post["json"]["audience"] for post in posts} == {audience.slug}
    assert {post["json"]["client"] for post in posts} == {api_client_record.slug}
    assert reasons_by_email == {
        "allowed@example.com": "category_unsubscribe",
        "suppressed@example.com": "hard_bounce",
    }

    replay = post_recipient_list_transactional(client, recipient_list.key, payload)

    assert replay.status_code == 202
    assert replay.json()["created_count"] == 0
    assert replay.json()["enqueued_count"] == 0
    assert replay.json()["idempotent_replay_count"] == 2
    assert TransactionalMessage.objects.count() == 2
    assert len(enqueued) == 0


def test_recipient_list_send_can_sync_members_and_render_member_context(
    client,
    audience,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    score_template = EmailTemplate.objects.create(
        client=api_client_record,
        key="homework-score-notification",
        name="Homework score notification",
        subject="Score: {{ total_score }}",
        html_body="<p>{{ homework_title }}: {{ total_score }}</p><p>{{ member.submission_id }}</p>",
        text_body="{{ homework_title }}: {{ total_score }} / {{ member.submission_id }}",
        required_context=[
            {"name": "homework_title"},
            {"name": "total_score"},
        ],
    )
    old_contact = Contact.objects.create(email="old@example.com")
    recipient_list = RecipientList.objects.create(
        client=api_client_record,
        audience=audience,
        key="ml-zoomcamp-2026:@e:@homework:homework-1",
        type="homework_submitters",
        name="Homework 1 submitters",
        member_count=1,
        active_member_count=1,
    )
    RecipientListMember.objects.create(
        recipient_list=recipient_list,
        contact=old_contact,
        email=old_contact.normalized_email,
        source_object_key="homework-submission:old",
    )

    payload = {
        "audience": audience.slug,
        "client": api_client_record.slug,
        "template_key": score_template.key,
        "idempotency_key": "homework-score:homework-1",
        "context": {
            "homework_title": "Homework 1",
        },
        "metadata": {"source": "score-publication"},
        "list": {
            "type": "homework_submitters",
            "name": "Homework 1 submitters",
            "metadata": {"course_slug": "ml-zoomcamp-2026"},
        },
        "members": [
            {
                "source_object_key": "homework-submission:1",
                "email": "learner@example.com",
                "status": "active",
                "metadata": {
                    "submission_id": 1,
                    "total_score": 9,
                    "course_slug": "ml-zoomcamp-2026",
                    "homework_slug": "homework-1",
                },
            }
        ],
    }

    response = post_recipient_list_transactional(client, recipient_list.key, payload)

    assert response.status_code == 202
    body = response.json()
    assert body["member_sync"]["upsert_count"] == 1
    assert body["member_sync"]["removed_count"] == 1
    assert body["created_count"] == 1
    message = TransactionalMessage.objects.get()
    assert message.email == "learner@example.com"
    assert message.context["total_score"] == 9
    assert message.context["member"]["submission_id"] == 1
    assert message.subject == "Score: 9"
    assert "Homework 1: 9" in message.text_body
    assert message.metadata["recipient_list_member_metadata"]["total_score"] == 9
    assert RecipientListMember.objects.get(source_object_key="homework-submission:old").active is False
    assert len(enqueued) == 1


def test_transient_recipient_list_send_renders_members_without_persisting_list(
    client,
    audience,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    reminder_template = EmailTemplate.objects.create(
        client=api_client_record,
        key="deadline-reminder",
        name="Deadline reminder",
        subject="{{ item_title }} due {{ deadline_at }}",
        html_body="<p>{{ item_title }} for {{ member.user_id }}</p>",
        text_body="{{ item_title }} / {{ deadline_at }} / {{ member.source_object_key }}",
        required_context=[
            {"name": "item_title"},
            {"name": "deadline_at"},
        ],
    )
    opted_out_contact = Contact.objects.create(email="opted-out@example.com")
    CategoryPreference.objects.create(
        contact=opted_out_contact,
        audience=audience,
        client=api_client_record,
        tag="deadline-reminders",
        label="Deadline reminders",
        enabled=False,
    )

    payload = {
        "audience": audience.slug,
        "client": api_client_record.slug,
        "template_key": reminder_template.key,
        "idempotency_key": "deadline-reminder:homework:1:24h",
        "category_tag": "deadline-reminders",
        "context": {
            "item_title": "Homework 1",
            "deadline_at": "Thursday, 18 June 2026, 01:00 Europe/Berlin",
        },
        "metadata": {
            "source": "course-management-platform",
            "event": "deadline_reminder",
        },
        "list": {
            "key": "deadline-reminders:homework:ml-zoomcamp-2026:homework-1:24h",
            "name": "Homework 1 24h deadline reminders",
            "metadata": {"deadline_kind": "homework"},
        },
        "members": [
            {
                "source_object_key": "enrollment:1",
                "email": "learner@example.com",
                "status": "active",
                "metadata": {
                    "user_id": 1,
                    "source_object_key": "enrollment:1",
                    "deadline_at": "Thursday, 18 June 2026, 01:00 Europe/Berlin",
                },
            },
            {
                "source_object_key": "enrollment:2",
                "email": "opted-out@example.com",
                "status": "active",
                "metadata": {
                    "user_id": 2,
                    "source_object_key": "enrollment:2",
                    "deadline_at": "Thursday, 18 June 2026, 01:00 Europe/Berlin",
                },
            },
        ],
    }

    response = post_transient_recipient_list_transactional(client, payload)

    assert response.status_code == 202
    body = response.json()
    assert body["transient_recipient_list"] == {
        "key": "deadline-reminders:homework:ml-zoomcamp-2026:homework-1:24h",
        "name": "Homework 1 24h deadline reminders",
        "member_count": 2,
        "active_member_count": 2,
    }
    assert body["created_count"] == 2
    assert body["enqueued_count"] == 1
    assert body["skipped_count"] == 1
    assert RecipientList.objects.count() == 0
    assert RecipientListMember.objects.count() == 0

    messages = list(TransactionalMessage.objects.order_by("email"))
    assert [message.email for message in messages] == ["learner@example.com", "opted-out@example.com"]
    assert messages[0].status == TransactionalMessageStatus.QUEUED
    assert messages[0].subject == "Homework 1 due Thursday, 18 June 2026, 01:00 Europe/Berlin"
    assert messages[0].context["member"]["user_id"] == 1
    assert messages[0].metadata["transient_recipient_list_key"] == payload["list"]["key"]
    assert messages[0].metadata["transient_member_metadata"]["user_id"] == 1
    assert messages[0].metadata["category_tag"] == "deadline-reminders"
    assert messages[1].status == TransactionalMessageStatus.SKIPPED
    assert messages[1].last_error == "category_unsubscribe"
    assert len(enqueued) == 1


def test_transactional_send_uses_client_default_sender(client, api_client_record, template, monkeypatch):
    api_client_record.default_sender_id = "courses"
    api_client_record.sender_emails = [
        {"id": "courses", "email": "courses@dtcdev.click"},
        {"id": "no-reply", "email": "no-reply@dtcdev.click"},
    ]
    api_client_record.save(update_fields=["default_sender_id", "sender_emails", "updated_at"])
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "context": {"product": "Datamailer"},
        },
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.from_email_id == "courses"
    assert message.from_email == "courses@dtcdev.click"
    assert response.json()["message"]["from_email"] == "courses"
    assert response.json()["message"]["from_email_address"] == "courses@dtcdev.click"
    assert len(enqueued) == 1


def test_transactional_send_uses_configured_display_sender(client, api_client_record, template, monkeypatch):
    api_client_record.default_sender_id = "courses"
    api_client_record.sender_emails = [
        {
            "id": "courses",
            "email": "DataTalks.Club Courses <courses@dtcdev.click>",
        },
    ]
    api_client_record.save(update_fields=["default_sender_id", "sender_emails", "updated_at"])
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", lambda payload: None)

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "context": {"product": "Datamailer"},
        },
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.from_email_id == "courses"
    assert message.from_email == "DataTalks.Club Courses <courses@dtcdev.click>"
    assert response.json()["message"]["from_email"] == "courses"
    assert response.json()["message"]["from_email_address"] == "DataTalks.Club Courses <courses@dtcdev.click>"


def test_client_sender_api_gets_and_updates_authenticated_client(client, api_client_record, template, monkeypatch):
    get_response = client.get(reverse("mailing:api_client_senders"), **auth_headers())

    assert get_response.status_code == 200
    assert get_response.json()["client"]["slug"] == "dtc-courses"
    assert get_response.json()["default_sender_id"] == "newsletter"
    assert get_response.json()["senders"] == [{"id": "newsletter", "email": "newsletter@example.com"}]

    update_response = put_client_senders(
        client,
        {
            "default_sender_id": "courses",
            "senders": [
                {
                    "id": "courses",
                    "email": "DataTalks.Club Courses <courses@dtcdev.click>",
                }
            ],
        },
    )

    assert update_response.status_code == 200
    assert update_response.json()["default_sender_id"] == "courses"
    assert update_response.json()["senders"] == [
        {
            "id": "courses",
            "email": "DataTalks.Club Courses <courses@dtcdev.click>",
        }
    ]
    api_client_record.refresh_from_db()
    assert api_client_record.default_sender_id == "courses"
    assert api_client_record.sender_emails == [
        {
            "id": "courses",
            "email": "DataTalks.Club Courses <courses@dtcdev.click>",
        }
    ]

    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", lambda payload: None)
    send_response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "context": {"product": "Datamailer"},
        },
    )

    assert send_response.status_code == 202
    assert send_response.json()["message"]["from_email"] == "courses"
    assert send_response.json()["message"]["from_email_address"] == "DataTalks.Club Courses <courses@dtcdev.click>"


def test_client_sender_api_validates_default_sender(client, api_client_record):
    response = put_client_senders(
        client,
        {
            "default_sender_id": "courses",
            "senders": [{"id": "newsletter", "email": "newsletter@example.com"}],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"default_sender_id": "not_configured"}


def test_transactional_send_accepts_allowed_payload_sender(client, api_client_record, template, monkeypatch):
    api_client_record.default_sender_id = "courses"
    api_client_record.sender_emails = [
        {"id": "courses", "email": "courses@dtcdev.click"},
        {"id": "no-reply", "email": "no-reply@dtcdev.click"},
    ]
    api_client_record.save(update_fields=["default_sender_id", "sender_emails", "updated_at"])
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", lambda payload: None)

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "from_email": "no-reply",
            "template_key": template.key,
            "context": {"product": "Datamailer"},
        },
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.from_email_id == "no-reply"
    assert message.from_email == "no-reply@dtcdev.click"
    assert response.json()["message"]["from_email"] == "no-reply"
    assert response.json()["message"]["from_email_address"] == "no-reply@dtcdev.click"


def test_transactional_send_rejects_unconfigured_payload_sender(client, api_client_record, template, monkeypatch):
    api_client_record.default_sender_id = "courses"
    api_client_record.sender_emails = [{"id": "courses", "email": "courses@dtcdev.click"}]
    api_client_record.save(update_fields=["default_sender_id", "sender_emails", "updated_at"])
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "from_email": "other",
            "template_key": template.key,
            "context": {"product": "Datamailer"},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"from_email": "not_configured"}
    assert TransactionalMessage.objects.count() == 0
    assert enqueued == []


def test_transactional_send_rejects_raw_payload_sender_email(client, api_client_record, template, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "from_email": "courses@dtcdev.click",
            "template_key": template.key,
            "context": {"product": "Datamailer"},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"from_email": "invalid"}
    assert TransactionalMessage.objects.count() == 0
    assert enqueued == []


def test_transactional_send_rejects_when_client_has_no_sender_config(client, api_client_record, template, monkeypatch):
    api_client_record.default_sender_id = ""
    api_client_record.sender_emails = []
    api_client_record.save(update_fields=["default_sender_id", "sender_emails", "updated_at"])
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "context": {"product": "Datamailer"},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"from_email": "not_configured"}
    assert TransactionalMessage.objects.count() == 0
    assert enqueued == []


def test_transactional_message_status_returns_message_and_events(
    client,
    template,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    send_response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "idempotency_key": "verify-123",
            "context": {"product": "Datamailer"},
        },
    )

    message_id = send_response.json()["message"]["id"]
    response = client.get(
        reverse("mailing:api_transactional_message_status", args=[message_id]),
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["message"]["id"] == message_id
    assert body["message"]["email"] == "person@example.com"
    assert body["message"]["from_email"] == "newsletter"
    assert body["message"]["from_email_address"] == "newsletter@example.com"
    assert body["message"]["status"] == TransactionalMessageStatus.QUEUED
    assert body["message"]["template_key"] == template.key
    assert body["message"]["idempotency_key"] == "verify-123"
    assert body["message"]["contact_id"] == Contact.objects.get().id
    assert body["events"][0]["event_type"] == EmailEventType.QUEUED
    assert body["events"][0]["transactional_message_id"] == message_id


def test_transactional_message_status_is_scoped_to_authenticated_client(
    client,
    template,
    other_client,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    send_response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "context": {"product": "Datamailer"},
        },
    )

    response = client.get(
        reverse(
            "mailing:api_transactional_message_status",
            args=[send_response.json()["message"]["id"]],
        ),
        **auth_headers("other-key"),
    )

    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"message_id": "not_found"}


def test_transactional_idempotency_reuses_existing_message_without_enqueue(client, template, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    payload = {
        "email": "person@example.com",
        "template_key": template.key,
        "idempotency_key": "same-request",
        "context": {"product": "Datamailer"},
    }

    first = post_transactional(client, payload)
    second = post_transactional(client, payload | {"metadata": {"ignored": True}})

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["message"]["id"] == first.json()["message"]["id"]
    assert second.json()["message"]["from_email"] == first.json()["message"]["from_email"]
    assert second.json()["idempotent_replay"] is True
    assert second.json()["enqueued"] is False
    assert TransactionalMessage.objects.count() == 1
    assert EmailEvent.objects.count() == 1
    assert len(enqueued) == 1


def test_transactional_required_context_rejects_before_mutation_or_enqueue(client, api_client_record, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    template = EmailTemplate.objects.create(
        client=api_client_record,
        key="password-reset",
        name="Password Reset",
        subject="Reset",
        text_body="Reset at {{ reset_url }}",
        required_context=[{"name": "reset_url", "description": "Client-generated reset URL."}],
        example_context={"reset_url": "https://client.example/reset/placeholder"},
    )

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "idempotency_key": "missing-context",
            "context": {},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"context.reset_url": "required"}
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


def test_transactional_idempotent_replay_does_not_revalidate_required_context(client, api_client_record, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    template = EmailTemplate.objects.create(
        client=api_client_record,
        key="email-verification",
        name="Email verification",
        subject="Verify",
        required_context=["verification_url"],
    )
    payload = {
        "email": "person@example.com",
        "template_key": template.key,
        "idempotency_key": "verify-replay",
        "context": {"verification_url": "https://client.example/verify/placeholder"},
    }

    first = post_transactional(client, payload)
    second = post_transactional(client, payload | {"context": {}})

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["idempotent_replay"] is True
    assert TransactionalMessage.objects.count() == 1
    assert len(enqueued) == 1


def test_transactional_send_does_not_require_verified_or_marketing_subscribed_contact(
    client,
    audience,
    template,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    contact = Contact.objects.create(email="person@example.com", global_unsubscribed_at=timezone.now())
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=template.client,
        status=SubscriptionStatus.UNSUBSCRIBED,
    )

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "context": {"product": "Datamailer"},
        },
    )

    assert response.status_code == 202
    assert TransactionalMessage.objects.get().status == TransactionalMessageStatus.QUEUED
    assert len(enqueued) == 1


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("hard_bounced_at", timezone.now(), "hard_bounce"),
        ("complained_at", timezone.now(), "complaint"),
    ],
)
def test_hard_suppressed_transactional_send_creates_skipped_audit_without_enqueue(
    client,
    template,
    monkeypatch,
    field,
    value,
    reason,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    Contact.objects.create(email="person@example.com", **{field: value})

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "idempotency_key": f"suppressed-{reason}",
        },
    )

    message = TransactionalMessage.objects.get()
    event = EmailEvent.objects.get()
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "transactional_suppressed"
    assert response.json()["error"]["reason"] == reason
    assert message.status == TransactionalMessageStatus.SKIPPED
    assert message.last_error == reason
    assert event.event_type == EmailEventType.SKIPPED
    assert event.metadata == {"reason": reason}
    assert enqueued == []


def test_transactional_template_key_is_scoped_to_authenticated_client(
    client,
    other_client,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    EmailTemplate.objects.create(
        client=other_client,
        key="password-reset",
        name="Other client reset",
        subject="Reset",
    )
    own_template = EmailTemplate.objects.create(
        client=api_client_record,
        key="password-reset",
        name="Own reset",
        subject="Reset {{ product }}",
    )

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": "password-reset",
            "idempotency_key": "reset-1",
            "context": {"product": "Datamailer"},
        },
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.template == own_template
    assert message.client == api_client_record
    assert enqueued[0]["template_id"] == own_template.id


def test_missing_or_campaign_only_template_returns_clear_404_without_mutation(
    client,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    EmailTemplate.objects.create(
        client=api_client_record,
        key="campaign-layout",
        name="Campaign layout",
        subject="News",
        is_transactional=False,
    )

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": "campaign-layout",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"template_key": "not_found"}
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


def test_inactive_transactional_template_returns_clear_404_without_mutation(
    client,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    EmailTemplate.objects.create(
        client=api_client_record,
        key="inactive",
        name="Inactive",
        subject="Inactive",
        is_active=False,
    )

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": "inactive",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"template_key": "not_found"}
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"template_key": "email-verification"}, "email"),
        ({"email": "not-email", "template_key": "email-verification"}, "email"),
        ({"email": "person@example.com"}, "template_key"),
        ({"email": "person@example.com", "template_key": "email-verification", "context": []}, "context"),
        ({"email": "person@example.com", "template_key": "email-verification", "metadata": []}, "metadata"),
        ({"email": "person@example.com", "template_key": "email-verification", "reply_to": "not-email"}, "reply_to"),
        (
            {"email": "person@example.com", "template_key": "email-verification", "idempotency_key": []},
            "idempotency_key",
        ),
    ],
)
def test_transactional_validation_errors_do_not_mutate(client, template, payload, field, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_transactional(client, payload)

    assert response.status_code == 400
    assert field in response.json()["error"]["fields"]
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


def test_transactional_models_are_available_in_admin():
    assert isinstance(admin.site._registry[EmailTemplate], EmailTemplateAdmin)
    assert isinstance(admin.site._registry[TransactionalMessage], TransactionalMessageAdmin)
    assert isinstance(admin.site._registry[EmailEvent], EmailEventAdmin)
