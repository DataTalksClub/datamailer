from dataclasses import dataclass
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from mailing.models import (
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.queue_contracts import CONTRACT_VERSION, TRANSACTIONAL_EMAIL_CONTRACT, validate_transactional_email_message
from mailing.services.api import ApiValidationError, isoformat
from mailing.services.contacts import is_transactional_email_allowed, normalize_email, upsert_contact
from mailing.services.senders import normalize_sender_id, resolve_sender_email
from mailing.services.transactional_catalog import validate_template_context
from mailing.services.transactional_rendering import render_template_string
from mailing.sqs import enqueue_transactional_email


class TransactionalSendRejected(Exception):
    def __init__(self, payload, *, status_code=409):
        self.payload = payload
        self.status_code = status_code
        super().__init__("transactional_send_rejected")


@dataclass(frozen=True)
class TransactionalSendResult:
    message: TransactionalMessage
    idempotent_replay: bool
    enqueued: bool


def send_transactional_email_for_client(data, authenticated_client):
    payload = validate_transactional_send_payload(data)
    template = get_transactional_template(authenticated_client, payload["template_key"])
    idempotency_key = payload["idempotency_key"] or build_internal_idempotency_key()

    existing = find_existing_message(authenticated_client, payload["idempotency_key"])
    if existing is not None:
        return response_payload(TransactionalSendResult(existing, idempotent_replay=True, enqueued=False))

    sender = resolve_sender_email(authenticated_client, payload["from_email"])
    validate_template_context(template, payload["context"])

    with transaction.atomic():
        contact, _ = upsert_contact(payload["email"])
        if is_transactional_email_allowed(contact):
            message = create_transactional_message(
                client=authenticated_client,
                contact=contact,
                template=template,
                payload=payload,
                sender=sender,
                idempotency_key=idempotency_key,
                status=TransactionalMessageStatus.QUEUED,
            )
            append_transactional_event(message, EmailEventType.QUEUED, {"template_key": template.key})
            queue_payload = build_transactional_queue_payload(message)
            transaction.on_commit(lambda: enqueue_transactional_email(queue_payload))

            return response_payload(TransactionalSendResult(message, idempotent_replay=False, enqueued=True))

        message = create_transactional_message(
            client=authenticated_client,
            contact=contact,
            template=template,
            payload=payload,
            sender=sender,
            idempotency_key=idempotency_key,
            status=TransactionalMessageStatus.SKIPPED,
            last_error=suppression_reason(contact),
        )
        append_transactional_event(message, EmailEventType.SKIPPED, {"reason": message.last_error})

    raise TransactionalSendRejected(
        response_payload(TransactionalSendResult(message, idempotent_replay=False, enqueued=False))
        | {
            "error": {
                "code": "transactional_suppressed",
                "message": "Contact is hard-suppressed for transactional email.",
                "reason": message.last_error,
            }
        },
        status_code=409,
    )


def validate_transactional_send_payload(data):
    errors = {}

    email = data.get("email")
    if not isinstance(email, str) or not email.strip():
        errors["email"] = "required"
    else:
        try:
            validate_email(email.strip())
        except ValidationError:
            errors["email"] = "invalid"

    template_key = data.get("template_key")
    if not isinstance(template_key, str) or not template_key.strip():
        errors["template_key"] = "required"

    idempotency_key = data.get("idempotency_key", "")
    if idempotency_key in (None, ""):
        idempotency_key = ""
    elif not isinstance(idempotency_key, str) or not idempotency_key.strip():
        errors["idempotency_key"] = "must_be_non_empty_string"
    else:
        idempotency_key = idempotency_key.strip()

    context = data.get("context", {})
    if context in (None, ""):
        context = {}
    elif not isinstance(context, dict):
        errors["context"] = "must_be_object"

    metadata = data.get("metadata", {})
    if metadata in (None, ""):
        metadata = {}
    elif not isinstance(metadata, dict):
        errors["metadata"] = "must_be_object"

    from_email = ""
    if "from_email" in data and data.get("from_email") not in (None, ""):
        try:
            from_email = normalize_sender_id(data.get("from_email"))
        except ApiValidationError as exc:
            errors.update(exc.errors)

    if errors:
        raise ApiValidationError(errors)

    return {
        "email": email.strip(),
        "template_key": template_key.strip(),
        "idempotency_key": idempotency_key,
        "context": context,
        "metadata": metadata,
        "from_email": from_email,
    }


def get_transactional_template(client, template_key):
    template = EmailTemplate.objects.filter(
        client=client,
        key=template_key,
        is_transactional=True,
        is_active=True,
    ).first()
    if template is None:
        raise ApiValidationError({"template_key": "not_found"}, status_code=404)
    return template


def find_existing_message(client, idempotency_key):
    if not idempotency_key:
        return None
    return (
        TransactionalMessage.objects.select_related("client", "contact", "template")
        .filter(client=client, idempotency_key=idempotency_key)
        .first()
    )


def build_internal_idempotency_key():
    return f"transactional-message:{uuid4().hex}"


def create_transactional_message(*, client, contact, template, payload, sender, idempotency_key, status, last_error=""):
    context = payload["context"]
    return TransactionalMessage.objects.create(
        client=client,
        contact=contact,
        email=normalize_email(payload["email"]),
        from_email_id=sender.sender_id,
        from_email=sender.email,
        template=template,
        template_key=template.key,
        status=status,
        idempotency_key=idempotency_key,
        subject=render_template_string(template.subject, context),
        html_body=render_template_string(template.html_body, context),
        text_body=render_template_string(template.text_body, context),
        context=context,
        metadata=payload["metadata"],
        last_error=last_error,
    )


def append_transactional_event(message, event_type, metadata):
    return EmailEvent.objects.create(
        transactional_message=message,
        contact=message.contact,
        client=message.client,
        event_type=event_type,
        metadata=metadata,
    )


def build_transactional_queue_payload(message):
    payload = {
        "contract": TRANSACTIONAL_EMAIL_CONTRACT,
        "version": CONTRACT_VERSION,
        "transactional_message_id": message.id,
        "client_id": message.client_id,
        "contact_id": message.contact_id,
        "template_id": message.template_id,
        "template_key": message.template_key,
        "idempotency_key": message.idempotency_key,
        "metadata": message.metadata,
    }
    return validate_transactional_email_message(payload)


def suppression_reason(contact):
    if contact.hard_bounced_at is not None:
        return "hard_bounce"
    if contact.complained_at is not None:
        return "complaint"
    return "suppressed"


def response_payload(result):
    message = result.message
    return {
        "message": {
            "id": message.id,
            "email": message.email,
            "from_email": message.from_email_id,
            "from_email_address": message.from_email,
            "status": message.status,
            "template_key": message.template_key,
            "idempotency_key": message.idempotency_key,
            "created_at": isoformat(message.created_at),
        },
        "idempotent_replay": result.idempotent_replay,
        "enqueued": result.enqueued,
    }
