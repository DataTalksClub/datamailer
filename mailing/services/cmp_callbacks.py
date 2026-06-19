import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction

from mailing.models import EmailEventType

logger = logging.getLogger(__name__)

CMP_EVENT_TYPES = {
    EmailEventType.BOUNCE: "contact.hard_bounced",
    EmailEventType.COMPLAINT: "contact.complained",
    EmailEventType.UNSUBSCRIBE: "subscription.unsubscribed",
    EmailEventType.SKIPPED: "transactional.skipped",
    EmailEventType.FAILED: "transactional.failed",
}


def cmp_callback_config():
    url = getattr(settings, "CMP_WEBHOOK_URL", "")
    token = getattr(settings, "CMP_WEBHOOK_TOKEN", "")
    if not url or not token:
        return None
    return {
        "url": url,
        "token": token,
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
    config = cmp_callback_config()
    if config is None:
        return

    payload = callback_payload(event)
    if payload is None:
        return

    transaction.on_commit(lambda: post_cmp_contact_event(config, payload))


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
    try:
        with urlopen(request, timeout=config["timeout"]):
            return
    except (HTTPError, URLError, OSError):
        logger.exception(
            "CMP contact event callback failed for event_id=%s",
            payload.get("event_id"),
        )
