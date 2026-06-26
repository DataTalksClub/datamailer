import json
from urllib.error import HTTPError, URLError

import pytest
from django.utils import timezone

from mailing.models import (
    Audience,
    Client,
    CmpCallback,
    CmpCallbackStatus,
    Contact,
    EmailEvent,
    EmailEventType,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services.api import upsert_contact_for_client
from mailing.services.cmp_callbacks import (
    enqueue_cmp_contact_event,
    process_due_cmp_callbacks,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
    )


@pytest.fixture
def app_client(organization):
    return Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
        cmp_webhook_url="https://cmp.example.com/api/datamailer/events",
        cmp_webhook_token="client-secret",
    )


@pytest.fixture
def contact():
    return Contact.objects.create(email="learner@example.com")


class Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def callback_event(contact, app_client, audience, event_type=EmailEventType.BOUNCE):
    return EmailEvent.objects.create(
        contact=contact,
        client=app_client,
        audience=audience,
        event_type=event_type,
        metadata={"source": "pytest"},
    )


def test_enqueue_cmp_callback_is_idempotent(contact, app_client, audience):
    event = callback_event(contact, app_client, audience)

    first = enqueue_cmp_contact_event(event.id)
    second = enqueue_cmp_contact_event(event.id)

    assert first.id == second.id
    assert CmpCallback.objects.count() == 1
    callback = CmpCallback.objects.get()
    assert callback.status == CmpCallbackStatus.PENDING
    assert callback.event_id == f"datamailer-email-event:{event.id}"
    assert callback.event_type == "contact.hard_bounced"
    assert callback.payload["email"] == "learner@example.com"
    assert callback.payload["audience"] == "dtc-courses"
    assert callback.payload["client"] == "dtc-courses"


@pytest.mark.parametrize(
    ("email_event_type", "callback_event_type"),
    [
        (EmailEventType.DELIVERED, "message.delivered"),
        (EmailEventType.OPEN, "message.opened"),
        (EmailEventType.CLICK, "message.clicked"),
    ],
)
def test_enqueue_cmp_callback_supports_lifecycle_events(
    contact,
    app_client,
    audience,
    email_event_type,
    callback_event_type,
):
    event = callback_event(
        contact,
        app_client,
        audience,
        event_type=email_event_type,
    )

    enqueue_cmp_contact_event(event.id)

    callback = CmpCallback.objects.get()
    assert callback.event_type == callback_event_type
    assert callback.payload["event_type"] == callback_event_type
    assert callback.payload["email"] == "learner@example.com"


def test_process_due_cmp_callbacks_delivers_pending_callback(monkeypatch, contact, app_client, audience):
    event = callback_event(contact, app_client, audience)
    enqueue_cmp_contact_event(event.id)
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
        return Response()

    monkeypatch.setattr("mailing.services.cmp_callbacks.urlopen", fake_urlopen)

    result = process_due_cmp_callbacks()

    assert result == {"processed": 1, "delivered": 1, "failed": 0}
    callback = CmpCallback.objects.get()
    assert callback.status == CmpCallbackStatus.DELIVERED
    assert callback.attempt_count == 1
    assert callback.delivered_at is not None
    assert posts[0]["url"] == "https://cmp.example.com/api/datamailer/events"
    assert posts[0]["headers"]["Authorization"] == "Bearer client-secret"
    assert posts[0]["json"]["event_type"] == "contact.hard_bounced"


def test_failed_callback_retries_and_then_delivers(monkeypatch, contact, app_client, audience):
    event = callback_event(contact, app_client, audience)
    callback = enqueue_cmp_contact_event(event.id)
    first_next_attempt = callback.next_attempt_at

    monkeypatch.setattr(
        "mailing.services.cmp_callbacks.urlopen",
        lambda request, *, timeout: (_ for _ in ()).throw(URLError("timeout")),
    )

    result = process_due_cmp_callbacks()

    callback.refresh_from_db()
    assert result == {"processed": 1, "delivered": 0, "failed": 1}
    assert callback.status == CmpCallbackStatus.PENDING
    assert callback.attempt_count == 1
    assert callback.next_attempt_at > first_next_attempt
    assert "timeout" in callback.last_error

    callback.next_attempt_at = timezone.now()
    callback.save(update_fields=["next_attempt_at", "updated_at"])
    monkeypatch.setattr("mailing.services.cmp_callbacks.urlopen", lambda request, *, timeout: Response())

    result = process_due_cmp_callbacks()

    callback.refresh_from_db()
    assert result == {"processed": 1, "delivered": 1, "failed": 0}
    assert callback.status == CmpCallbackStatus.DELIVERED
    assert callback.attempt_count == 2


def test_callback_stops_retrying_after_max_attempts(monkeypatch, contact, app_client, audience):
    event = callback_event(contact, app_client, audience)
    callback = enqueue_cmp_contact_event(event.id)
    callback.max_attempts = 1
    callback.save(update_fields=["max_attempts", "updated_at"])

    def fail_with_http_error(request, *, timeout):
        raise HTTPError(request.full_url, 500, "server error", hdrs=None, fp=None)

    monkeypatch.setattr("mailing.services.cmp_callbacks.urlopen", fail_with_http_error)

    result = process_due_cmp_callbacks()

    callback.refresh_from_db()
    assert result == {"processed": 1, "delivered": 0, "failed": 1}
    assert callback.status == CmpCallbackStatus.FAILED
    assert callback.attempt_count == 1
    assert callback.response_status == 500
    assert "server error" in callback.last_error


def test_resubscribe_api_creates_cmp_callback(contact, app_client, audience):
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=app_client,
        status=SubscriptionStatus.UNSUBSCRIBED,
        unsubscribed_at=timezone.now(),
        unsubscribe_reason="test",
    )

    upsert_contact_for_client(
        {
            "email": contact.email,
            "audience": audience.slug,
            "client": app_client.slug,
            "status": SubscriptionStatus.SUBSCRIBED,
        },
        app_client,
    )

    callback = CmpCallback.objects.get()
    assert callback.event_type == "subscription.resubscribed"
    assert callback.payload["event_type"] == "subscription.resubscribed"
    assert callback.payload["email"] == "learner@example.com"
