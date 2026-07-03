"""One-way Datamailer -> Mailchimp contact/tag sync.

When a contact becomes an active member of a recipient-list tree node, Datamailer
pushes the contact into the client's Mailchimp audience and applies the tag mapped
to that node (see :class:`~mailing.models.MailchimpTagMapping`). Delivery uses an
outbox (:class:`~mailing.models.MailchimpSync`) with exponential backoff, mirroring
the CMP callback dispatcher.

Configuration is per client and write-only from the client's perspective: the API
lets a client *set* its Mailchimp key, audience id, and tag mappings, but never
returns the stored key.
"""

import base64
import hashlib
import json
import logging
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mailing.models import (
    Client,
    MailchimpSync,
    MailchimpSyncStatus,
    MailchimpTagMapping,
)
from mailing.services.api_errors import ApiValidationError

logger = logging.getLogger(__name__)

RETRY_DELAYS_SECONDS = [60, 300, 900, 3600, 10800, 21600, 43200]


def derive_datacenter(api_key):
    """Mailchimp keys end in ``-<datacenter>`` (e.g. ``abc123...-us21``)."""
    key = (api_key or "").strip()
    if "-" not in key:
        return ""
    return key.rsplit("-", 1)[-1].strip()


def mailchimp_config(client):
    if client is None or not client.mailchimp_enabled:
        return None
    api_key = (client.mailchimp_api_key or "").strip()
    list_id = (client.mailchimp_list_id or "").strip()
    datacenter = derive_datacenter(api_key)
    if not api_key or not list_id or not datacenter:
        return None
    return {
        "api_key": api_key,
        "list_id": list_id,
        "datacenter": datacenter,
        "timeout": getattr(settings, "MAILCHIMP_TIMEOUT_SECONDS", 5.0),
    }


def subscriber_hash(email):
    return hashlib.md5(email.strip().casefold().encode("utf-8")).hexdigest()


def dedup_key_for(client, list_key, contact_id, tag):
    return f"{client.id}:{list_key}:{contact_id}:{tag}"


# --- Enqueue ---------------------------------------------------------------


def tag_mappings_for_node(client, audience, list_key):
    return MailchimpTagMapping.objects.filter(
        client=client,
        audience=audience,
        list_key=list_key,
        enabled=True,
    )


def emit_mailchimp_syncs_for_node(client, audience, contact, email, list_key):
    """Queue a sync for each enabled tag mapped to ``list_key``.

    No-op when Mailchimp is not configured/enabled for the client or when no tag
    is mapped to the node. Enqueue happens after the surrounding DB transaction
    commits, matching the CMP callback pattern.
    """
    if mailchimp_config(client) is None:
        return
    tags = list(
        tag_mappings_for_node(client, audience, list_key).values_list("tag", flat=True)
    )
    if not tags:
        return
    contact_id = contact.id
    for tag in tags:
        transaction.on_commit(
            lambda tag=tag: enqueue_mailchimp_sync(
                client_id=client.id,
                audience_id=audience.id if audience is not None else None,
                contact_id=contact_id,
                email=email,
                list_key=list_key,
                tag=tag,
            )
        )


def enqueue_mailchimp_sync(*, client_id, audience_id, contact_id, email, list_key, tag):
    client = Client.objects.filter(pk=client_id).first()
    config = mailchimp_config(client)
    if config is None:
        return None

    dedup_key = dedup_key_for(client, list_key, contact_id, tag)
    sync, created = MailchimpSync.objects.get_or_create(
        dedup_key=dedup_key,
        defaults={
            "client_id": client_id,
            "audience_id": audience_id,
            "contact_id": contact_id,
            "email": email,
            "list_key": list_key,
            "tag": tag,
            "mailchimp_list_id": config["list_id"],
            "next_attempt_at": timezone.now(),
        },
    )
    if not created and sync.status != MailchimpSyncStatus.PENDING:
        # A previously delivered/failed grant is being re-applied (e.g. the
        # contact was removed and re-added). Reopen it for another attempt.
        sync.status = MailchimpSyncStatus.PENDING
        sync.attempt_count = 0
        sync.next_attempt_at = timezone.now()
        sync.last_error = ""
        sync.mailchimp_list_id = config["list_id"]
        sync.save(
            update_fields=[
                "status",
                "attempt_count",
                "next_attempt_at",
                "last_error",
                "mailchimp_list_id",
                "updated_at",
            ]
        )
    return sync


# --- Dispatch --------------------------------------------------------------


def due_mailchimp_syncs(*, limit=25, now=None):
    now = now or timezone.now()
    return (
        MailchimpSync.objects.select_related("client", "contact", "audience")
        .filter(status=MailchimpSyncStatus.PENDING, next_attempt_at__lte=now)
        .order_by("next_attempt_at", "id")[:limit]
    )


def process_due_mailchimp_syncs(*, limit=25, now=None):
    processed = 0
    delivered = 0
    failed = 0
    for sync in due_mailchimp_syncs(limit=limit, now=now):
        processed += 1
        if dispatch_mailchimp_sync(sync):
            delivered += 1
        else:
            failed += 1
    return {"processed": processed, "delivered": delivered, "failed": failed}


def dispatch_mailchimp_sync(sync):
    config = mailchimp_config(sync.client)
    if config is None:
        mark_mailchimp_sync_failed(
            sync,
            "Mailchimp is not configured/enabled for this client.",
            response_status=None,
            permanent=True,
        )
        return False

    try:
        upsert_mailchimp_member(config, sync.email)
        add_mailchimp_member_tag(config, sync.email, sync.tag)
    except HTTPError as exc:
        # 4xx (except 429) are permanent: bad key, unknown list, invalid email.
        permanent = exc.code not in (429,) and 400 <= exc.code < 500
        mark_mailchimp_sync_failed(sync, _http_error_detail(exc), response_status=exc.code, permanent=permanent)
        logger.warning("Mailchimp sync failed (%s) for %s: %s", exc.code, sync.email, sync.last_error)
        return False
    except (URLError, OSError) as exc:
        mark_mailchimp_sync_failed(sync, str(exc), response_status=None)
        logger.warning("Mailchimp sync transport error for %s: %s", sync.email, exc)
        return False

    mark_mailchimp_sync_delivered(sync)
    return True


def _mailchimp_base_url(config):
    return f"https://{config['datacenter']}.api.mailchimp.com/3.0"


def _auth_header(config):
    token = base64.b64encode(f"anystring:{config['api_key']}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _mailchimp_request(config, method, path, payload):
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        f"{_mailchimp_base_url(config)}{path}",
        data=data,
        headers={
            "Authorization": _auth_header(config),
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urlopen(request, timeout=config["timeout"]):
        return


def upsert_mailchimp_member(config, email):
    _mailchimp_request(
        config,
        "PUT",
        f"/lists/{config['list_id']}/members/{subscriber_hash(email)}",
        {"email_address": email, "status_if_new": "subscribed"},
    )


def add_mailchimp_member_tag(config, email, tag):
    _mailchimp_request(
        config,
        "POST",
        f"/lists/{config['list_id']}/members/{subscriber_hash(email)}/tags",
        {"tags": [{"name": tag, "status": "active"}]},
    )


def _http_error_detail(exc):
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:  # pragma: no cover - defensive
        body = ""
    detail = f"HTTP {exc.code}"
    if body:
        detail = f"{detail}: {body[:500]}"
    return detail


def mark_mailchimp_sync_delivered(sync):
    now = timezone.now()
    sync.status = MailchimpSyncStatus.DELIVERED
    sync.attempt_count += 1
    sync.last_attempt_at = now
    sync.delivered_at = now
    sync.response_status = 200
    sync.last_error = ""
    sync.save(
        update_fields=[
            "status",
            "attempt_count",
            "last_attempt_at",
            "delivered_at",
            "response_status",
            "last_error",
            "updated_at",
        ]
    )


def mark_mailchimp_sync_failed(sync, error, *, response_status=None, permanent=False):
    now = timezone.now()
    sync.attempt_count += 1
    sync.last_attempt_at = now
    sync.response_status = response_status
    sync.last_error = error[:2000]
    if permanent or sync.attempt_count >= sync.max_attempts:
        sync.status = MailchimpSyncStatus.FAILED
    else:
        sync.status = MailchimpSyncStatus.PENDING
        sync.next_attempt_at = now + retry_delay(sync.attempt_count)
    sync.save(
        update_fields=[
            "status",
            "attempt_count",
            "next_attempt_at",
            "last_attempt_at",
            "response_status",
            "last_error",
            "updated_at",
        ]
    )


def retry_delay(attempt_count):
    delay_index = max(0, min(attempt_count - 1, len(RETRY_DELAYS_SECONDS) - 1))
    return timedelta(seconds=RETRY_DELAYS_SECONDS[delay_index])


# --- Client-facing configuration (set-only) --------------------------------


def mailchimp_status_payload(client):
    """Non-secret status. The stored API key is never returned."""
    return {
        "client": client.slug,
        "enabled": client.mailchimp_enabled,
        "configured": mailchimp_config(client) is not None,
        "list_id": client.mailchimp_list_id,
        "datacenter": derive_datacenter(client.mailchimp_api_key),
        "api_key_set": bool((client.mailchimp_api_key or "").strip()),
    }


def validate_mailchimp_config_payload(data, client):
    if not isinstance(data, dict):
        raise ApiValidationError({"body": "must_be_object"})

    errors = {}
    updates = {}

    if "api_key" in data:
        api_key = data.get("api_key")
        if api_key in (None, ""):
            updates["mailchimp_api_key"] = ""
        elif not isinstance(api_key, str):
            errors["api_key"] = "must_be_string"
        elif not derive_datacenter(api_key):
            # Mailchimp keys embed the datacenter as a `-usXX` suffix.
            errors["api_key"] = "missing_datacenter_suffix"
        else:
            updates["mailchimp_api_key"] = api_key.strip()

    if "list_id" in data:
        list_id = data.get("list_id")
        if list_id in (None, ""):
            updates["mailchimp_list_id"] = ""
        elif not isinstance(list_id, str):
            errors["list_id"] = "must_be_string"
        else:
            updates["mailchimp_list_id"] = list_id.strip()

    if "enabled" in data:
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            errors["enabled"] = "must_be_boolean"
        else:
            updates["mailchimp_enabled"] = enabled

    if errors:
        raise ApiValidationError(errors)
    return updates


def update_mailchimp_config_for_client(data, client):
    updates = validate_mailchimp_config_payload(data, client)
    if updates:
        for field, value in updates.items():
            setattr(client, field, value)
        client.save(update_fields=[*updates.keys(), "updated_at"])
    return mailchimp_status_payload(client)


# --- Tag mappings (set-only reconcile) -------------------------------------


def tag_mapping_payload(mapping):
    return {
        "audience": mapping.audience.slug,
        "list_key": mapping.list_key,
        "tag": mapping.tag,
        "enabled": mapping.enabled,
    }


def tag_mappings_payload(client, audience):
    mappings = MailchimpTagMapping.objects.filter(client=client, audience=audience).order_by("list_key", "tag")
    return {
        "client": client.slug,
        "audience": audience.slug,
        "mappings": [tag_mapping_payload(mapping) for mapping in mappings],
    }


def validate_tag_mappings_payload(data):
    if not isinstance(data, dict):
        raise ApiValidationError({"body": "must_be_object"})

    mappings = data.get("mappings")
    if not isinstance(mappings, list):
        raise ApiValidationError({"mappings": "must_be_list"})

    errors = {}
    normalized = []
    seen = set()
    for index, mapping in enumerate(mappings):
        if not isinstance(mapping, dict):
            errors[f"mappings.{index}"] = "must_be_object"
            continue
        list_key = mapping.get("list_key")
        if not isinstance(list_key, str) or not list_key.strip():
            errors[f"mappings.{index}.list_key"] = "required"
            continue
        tag = mapping.get("tag")
        if not isinstance(tag, str) or not tag.strip():
            errors[f"mappings.{index}.tag"] = "required"
            continue
        enabled = mapping.get("enabled", True)
        if not isinstance(enabled, bool):
            errors[f"mappings.{index}.enabled"] = "must_be_boolean"
            continue
        key = (list_key.strip(), tag.strip())
        if key in seen:
            errors[f"mappings.{index}"] = "duplicate"
            continue
        seen.add(key)
        normalized.append({"list_key": list_key.strip(), "tag": tag.strip(), "enabled": enabled})

    if errors:
        raise ApiValidationError(errors)
    return normalized


@transaction.atomic
def reconcile_tag_mappings_for_client(audience, data, client):
    """Replace the client's tag mappings for ``audience`` with the payload set."""
    normalized = validate_tag_mappings_payload(data)
    desired_keys = {(item["list_key"], item["tag"]) for item in normalized}

    for existing in MailchimpTagMapping.objects.filter(client=client, audience=audience):
        if (existing.list_key, existing.tag) not in desired_keys:
            existing.delete()

    for item in normalized:
        MailchimpTagMapping.objects.update_or_create(
            client=client,
            audience=audience,
            list_key=item["list_key"],
            tag=item["tag"],
            defaults={"enabled": item["enabled"]},
        )
    return tag_mappings_payload(client, audience)
