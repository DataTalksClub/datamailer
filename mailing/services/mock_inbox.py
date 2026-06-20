"""Mock inbox: a capture-only test mailbox for end-to-end tests.

Datamailer already persists every transactional send as a ``TransactionalMessage``
row containing the rendered subject/body, template key, context, metadata and
idempotency key. The mock inbox does not introduce a new store; it adds:

* :func:`is_mock_address` - recognise a designated test address so the worker can
  skip real SES delivery for it (see ``transactional_sender``);
* read/clear helpers over the existing ``TransactionalMessage`` rows, scoped to a
  single mock address, used by the mock-inbox API endpoints.

This keeps the test path deterministic (no real mail, no SES inbound dependency)
while reusing the exact data the platform already records.
"""

from django.conf import settings

from mailing.models import TransactionalMessage
from mailing.services.api import ApiValidationError, isoformat
from mailing.services.contacts import normalize_email

DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def mock_inbox_enabled():
    return bool(getattr(settings, "MOCK_INBOX_ENABLED", False))


def _mock_domain():
    return (getattr(settings, "MOCK_INBOX_DOMAIN", "") or "").strip().lower()


def _plus_tag():
    return (getattr(settings, "MOCK_INBOX_PLUS_TAG", "") or "").strip().lower()


def is_mock_address(email):
    """Return True when ``email`` should be captured instead of really delivered.

    An address is a mock address when the mock inbox is enabled and either:

    * its domain equals ``MOCK_INBOX_DOMAIN`` (e.g. ``anyone@mailbox.test``), or
    * its local part is sub-addressed with ``MOCK_INBOX_PLUS_TAG`` (e.g.
      ``e2e+homework@example.com``).
    """
    if not mock_inbox_enabled():
        return False
    if not isinstance(email, str) or "@" not in email:
        return False

    normalized = normalize_email(email)
    local, _, domain = normalized.partition("@")

    domain_match = bool(_mock_domain()) and domain == _mock_domain()

    tag = _plus_tag()
    base_local = local.split("+", 1)[0]
    plus_segment = local[len(base_local) + 1 :] if "+" in local else ""
    plus_match = bool(tag) and (base_local == tag or plus_segment.split("+", 1)[0] == tag)

    return domain_match or plus_match


def _validate_limit(raw_limit):
    if raw_limit in (None, ""):
        return DEFAULT_LIST_LIMIT
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ApiValidationError({"limit": "must_be_integer"}) from exc
    if limit < 1:
        raise ApiValidationError({"limit": "must_be_positive"})
    return min(limit, MAX_LIST_LIMIT)


def _require_address(data):
    address = data.get("address") or data.get("email")
    if not isinstance(address, str) or not address.strip():
        raise ApiValidationError({"address": "required"})
    return normalize_email(address)


def message_summary(message):
    return {
        "id": message.id,
        "email": message.email,
        "from_email": message.from_email,
        "subject": message.subject,
        "template_key": message.template_key,
        "status": message.status,
        "idempotency_key": message.idempotency_key,
        "created_at": isoformat(message.created_at),
    }


def message_detail(message):
    return message_summary(message) | {
        "html_body": message.html_body,
        "text_body": message.text_body,
        "context": message.context,
        "metadata": message.metadata,
    }


def _messages_for_address(authenticated_client, address):
    return TransactionalMessage.objects.filter(
        client=authenticated_client,
        email=address,
    )


def list_mock_inbox_messages(data, authenticated_client):
    """List recently captured messages for a single mock address.

    Newest first. Accepts ``address`` (or ``email``) and an optional ``limit``.
    Raises :class:`ApiValidationError` (422) when the address is not a mock
    address, so a misconfigured test fails loudly instead of silently polling an
    address that is delivered for real.
    """
    address = _require_address(data)
    if not is_mock_address(address):
        raise ApiValidationError({"address": "not_a_mock_address"}, status_code=422)
    limit = _validate_limit(data.get("limit"))

    messages = _messages_for_address(authenticated_client, address).order_by("-created_at", "-id")[:limit]
    items = [message_summary(message) for message in messages]
    return {
        "address": address,
        "count": len(items),
        "messages": items,
    }


def get_mock_inbox_message(message_id, authenticated_client):
    message = (
        TransactionalMessage.objects.filter(id=message_id, client=authenticated_client)
        .select_related("client", "contact")
        .first()
    )
    if message is None or not is_mock_address(message.email):
        raise ApiValidationError({"message_id": "not_found"}, status_code=404)
    return {"message": message_detail(message)}


def clear_mock_inbox(data, authenticated_client):
    """Delete captured messages for a mock address (test teardown).

    Without an ``address`` it clears every captured mock message for the client.
    Related ``EmailEvent`` rows cascade-delete with the message.
    """
    address = data.get("address") or data.get("email")
    queryset = TransactionalMessage.objects.filter(client=authenticated_client)

    if isinstance(address, str) and address.strip():
        normalized = normalize_email(address)
        if not is_mock_address(normalized):
            raise ApiValidationError({"address": "not_a_mock_address"}, status_code=422)
        queryset = queryset.filter(email=normalized)
        scope = normalized
    else:
        # Clear all mock-addressed messages: filter in Python because mock-ness
        # is a settings-driven predicate, not a single SQL expression.
        ids = [message.id for message in queryset.only("id", "email") if is_mock_address(message.email)]
        queryset = TransactionalMessage.objects.filter(id__in=ids)
        scope = None

    deleted_count, _ = queryset.delete()
    return {"address": scope, "deleted_count": deleted_count}
