from django.conf import settings

from mailing.models import CapturedEmail
from mailing.services.contacts import normalize_email

DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def capture_mode_enabled():
    return getattr(settings, "DATAMAILER_DELIVERY_MODE", "send") == "capture"


def capture_api_enabled():
    return bool(getattr(settings, "DATAMAILER_CAPTURE_UI", False)) or capture_mode_enabled()


def capture_transactional_message(message, *, source, metadata=None):
    capture = CapturedEmail.objects.create(
        client=message.client,
        contact=message.contact,
        transactional_message=message,
        email=normalize_email(message.email),
        from_email=message.from_email,
        subject=message.subject,
        html_body=message.html_body,
        text_body=message.text_body,
        template_key=message.template_key,
        source=_metadata_value(message.metadata, "source") or source,
        event=_metadata_value(message.metadata, "event"),
        idempotency_key=message.idempotency_key,
        metadata=(message.metadata or {}) | (metadata or {}),
    )
    return capture


def capture_campaign_recipient(recipient, *, rendered, source, metadata=None):
    campaign = recipient.campaign
    capture = CapturedEmail.objects.create(
        client=campaign.client,
        audience=campaign.audience,
        contact=recipient.contact,
        campaign=campaign,
        campaign_recipient=recipient,
        email=normalize_email(recipient.email),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", ""),
        subject=rendered["subject"],
        html_body=rendered["html_body"],
        text_body=rendered["text_body"],
        template_key="",
        source=source,
        event="campaign",
        idempotency_key=f"campaign-recipient:{recipient.id}",
        metadata={
            "campaign_id": campaign.id,
            "campaign_external_key": campaign.external_key,
            "campaign_recipient_id": recipient.id,
        }
        | (metadata or {}),
    )
    return capture


def captured_email_summary(capture):
    return {
        "id": capture.id,
        "email": capture.email,
        "from_email": capture.from_email,
        "subject": capture.subject,
        "template_key": capture.template_key,
        "source": capture.source,
        "event": capture.event,
        "idempotency_key": capture.idempotency_key,
        "created_at": isoformat(capture.created_at),
    }


def captured_email_detail(capture):
    return captured_email_summary(capture) | {
        "html_body": capture.html_body,
        "text_body": capture.text_body,
        "metadata": capture.metadata,
        "transactional_message_id": capture.transactional_message_id,
        "campaign_id": capture.campaign_id,
        "campaign_recipient_id": capture.campaign_recipient_id,
    }


def list_captured_emails(data, authenticated_client):
    queryset = CapturedEmail.objects.filter(client=authenticated_client).order_by("-created_at", "-id")

    email = data.get("email") or data.get("address")
    if isinstance(email, str) and email.strip():
        queryset = queryset.filter(email=normalize_email(email))

    source = data.get("source")
    if isinstance(source, str) and source.strip():
        queryset = queryset.filter(source=source.strip())

    event = data.get("event")
    if isinstance(event, str) and event.strip():
        queryset = queryset.filter(event=event.strip())

    limit = _validate_limit(data.get("limit"))
    items = [captured_email_summary(capture) for capture in queryset[:limit]]
    return {"count": len(items), "runs": items}


def get_captured_email(capture_id, authenticated_client):
    capture = CapturedEmail.objects.filter(id=capture_id, client=authenticated_client).first()
    if capture is None:
        from mailing.services.api import ApiValidationError

        raise ApiValidationError({"run_id": "not_found"}, status_code=404)
    return {"run": captured_email_detail(capture)}


def get_captured_run_message(run_id, message_id, authenticated_client):
    if run_id != message_id:
        from mailing.services.api import ApiValidationError

        raise ApiValidationError({"message_id": "not_found"}, status_code=404)
    capture = CapturedEmail.objects.filter(id=run_id, client=authenticated_client).first()
    if capture is None:
        from mailing.services.api import ApiValidationError

        raise ApiValidationError({"run_id": "not_found"}, status_code=404)
    return {"message": captured_email_detail(capture)}


def clear_captured_emails(data, authenticated_client):
    queryset = CapturedEmail.objects.filter(client=authenticated_client)

    email = data.get("email") or data.get("address")
    if isinstance(email, str) and email.strip():
        queryset = queryset.filter(email=normalize_email(email))

    deleted_count, _ = queryset.delete()
    return {"deleted_count": deleted_count}


def _metadata_value(metadata, key):
    value = (metadata or {}).get(key, "")
    return value if isinstance(value, str) else ""


def _validate_limit(raw_limit):
    if raw_limit in (None, ""):
        return DEFAULT_LIST_LIMIT
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        from mailing.services.api import ApiValidationError

        raise ApiValidationError({"limit": "must_be_integer"}) from exc
    if limit < 1:
        from mailing.services.api import ApiValidationError

        raise ApiValidationError({"limit": "must_be_positive"})
    return min(limit, MAX_LIST_LIMIT)


def isoformat(value):
    return value.isoformat() if value else None
