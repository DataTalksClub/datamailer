import json
import logging
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mailing.models import CmpCallback, CmpCallbackStatus, EmailEvent, EmailEventType

logger = logging.getLogger(__name__)

CMP_EVENT_TYPES = {
    EmailEventType.SUBSCRIBE: "subscription.resubscribed",
    EmailEventType.BOUNCE: "contact.hard_bounced",
    EmailEventType.COMPLAINT: "contact.complained",
    EmailEventType.UNSUBSCRIBE: "subscription.unsubscribed",
    EmailEventType.SKIPPED: "transactional.skipped",
    EmailEventType.FAILED: "transactional.failed",
}

RETRY_DELAYS_SECONDS = [60, 300, 900, 3600, 10800, 21600, 43200]


def cmp_callback_config(client=None):
    url = ""
    token = ""
    if client is not None:
        url = (client.cmp_webhook_url or "").strip()
        token = (client.cmp_webhook_token or "").strip()
    if not url or not token:
        url = getattr(settings, "CMP_WEBHOOK_URL", "")
        token = getattr(settings, "CMP_WEBHOOK_TOKEN", "")
    if not url or not token:
        return None
    return {
        "url": url.strip(),
        "token": token.strip(),
        "timeout": getattr(settings, "CMP_WEBHOOK_TIMEOUT_SECONDS", 3.0),
    }


def callback_metadata(event):
    metadata = dict(event.metadata or {})
    if event.transactional_message_id:
        metadata = {
            **(event.transactional_message.metadata or {}),
            **metadata,
        }
    if event.campaign_id:
        metadata["campaign_id"] = event.campaign_id
    if event.campaign_recipient_id:
        metadata["campaign_recipient_id"] = event.campaign_recipient_id
    if event.transactional_message_id:
        metadata["transactional_message_id"] = event.transactional_message_id
    if event.provider_event_id:
        metadata["provider_event_id"] = event.provider_event_id
    return metadata


def callback_payload(event):
    if event.event_type not in CMP_EVENT_TYPES or event.contact is None:
        return None
    if (
        event.event_type
        in {
            EmailEventType.SKIPPED,
            EmailEventType.FAILED,
        }
        and not event.transactional_message_id
    ):
        return None

    metadata = callback_metadata(event)
    preference_key = metadata.get("preference_key") or metadata.get("cmp_preference_key") or ""
    payload = {
        "event_id": f"datamailer-email-event:{event.pk}",
        "event_type": CMP_EVENT_TYPES[event.event_type],
        "email": event.contact.normalized_email,
        "occurred_at": event.created_at.isoformat(),
        "contact_id": event.contact_id,
        "email_event_id": event.pk,
        "audience": event.audience.slug if event.audience_id else metadata.get("audience", ""),
        "client": event.client.slug if event.client_id else "",
        "metadata": metadata,
    }
    if preference_key:
        payload["preference_key"] = preference_key
    return payload


def emit_cmp_contact_event(event):
    payload = callback_payload(event)
    if payload is None:
        return

    transaction.on_commit(lambda: enqueue_cmp_contact_event(event.pk))


def enqueue_cmp_contact_event(email_event_id):
    event = (
        EmailEvent.objects.select_related(
            "contact",
            "client",
            "audience",
            "campaign",
            "campaign_recipient",
            "transactional_message",
        )
        .filter(pk=email_event_id)
        .first()
    )
    if event is None:
        return None

    payload = callback_payload(event)
    if payload is None:
        return None

    config = cmp_callback_config(event.client)
    if config is None:
        return None

    callback, _ = CmpCallback.objects.get_or_create(
        email_event=event,
        defaults={
            "contact": event.contact,
            "client": event.client,
            "audience": event.audience,
            "event_id": payload["event_id"],
            "event_type": payload["event_type"],
            "callback_url": config["url"],
            "payload": payload,
            "next_attempt_at": timezone.now(),
        },
    )
    return callback


def due_cmp_callbacks(*, limit=25, now=None):
    now = now or timezone.now()
    return (
        CmpCallback.objects.select_related("client", "contact", "audience", "email_event")
        .filter(status=CmpCallbackStatus.PENDING, next_attempt_at__lte=now)
        .order_by("next_attempt_at", "id")[:limit]
    )


def process_due_cmp_callbacks(*, limit=25, now=None):
    processed = 0
    delivered = 0
    failed = 0
    for callback in due_cmp_callbacks(limit=limit, now=now):
        processed += 1
        if dispatch_cmp_callback(callback):
            delivered += 1
        else:
            failed += 1
    return {
        "processed": processed,
        "delivered": delivered,
        "failed": failed,
    }


def dispatch_cmp_callback(callback):
    config = cmp_callback_config(callback.client)
    if config is None:
        mark_cmp_callback_failed(
            callback,
            "CMP webhook is not configured for this client.",
            response_status=None,
        )
        return False

    callback.callback_url = config["url"]
    try:
        post_cmp_contact_event(config, callback.payload)
    except (HTTPError, URLError, OSError) as exc:
        response_status = exc.code if isinstance(exc, HTTPError) else None
        mark_cmp_callback_failed(callback, str(exc), response_status=response_status)
        logger.exception(
            "CMP contact event callback failed for event_id=%s",
            callback.event_id,
        )
        return False

    mark_cmp_callback_delivered(callback)
    return True


def post_cmp_contact_event(config, payload):
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        config["url"],
        data=data,
        headers={
            "Authorization": f"Bearer {config['token']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=config["timeout"]):
        return


def mark_cmp_callback_delivered(callback):
    now = timezone.now()
    callback.status = CmpCallbackStatus.DELIVERED
    callback.attempt_count += 1
    callback.last_attempt_at = now
    callback.delivered_at = now
    callback.response_status = 200
    callback.last_error = ""
    callback.save(
        update_fields=[
            "status",
            "attempt_count",
            "last_attempt_at",
            "delivered_at",
            "response_status",
            "last_error",
            "callback_url",
            "updated_at",
        ]
    )


def mark_cmp_callback_failed(callback, error, *, response_status=None):
    now = timezone.now()
    callback.attempt_count += 1
    callback.last_attempt_at = now
    callback.response_status = response_status
    callback.last_error = error[:2000]
    if callback.attempt_count >= callback.max_attempts:
        callback.status = CmpCallbackStatus.FAILED
    else:
        callback.status = CmpCallbackStatus.PENDING
        callback.next_attempt_at = now + retry_delay(callback.attempt_count)
    callback.save(
        update_fields=[
            "status",
            "attempt_count",
            "next_attempt_at",
            "last_attempt_at",
            "response_status",
            "last_error",
            "callback_url",
            "updated_at",
        ]
    )


def retry_delay(attempt_count):
    delay_index = max(0, min(attempt_count - 1, len(RETRY_DELAYS_SECONDS) - 1))
    return timedelta(seconds=RETRY_DELAYS_SECONDS[delay_index])
