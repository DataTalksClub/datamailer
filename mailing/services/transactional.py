from dataclasses import dataclass
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from mailing.models import (
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    RecipientList,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.queue_contracts import CONTRACT_VERSION, TRANSACTIONAL_EMAIL_CONTRACT, validate_transactional_email_message
from mailing.services.api import ApiValidationError, isoformat, validate_contact_scope
from mailing.services.cmp_callbacks import emit_cmp_contact_event
from mailing.services.contacts import is_transactional_email_allowed, normalize_email, upsert_contact
from mailing.services.recipient_lists import (
    bulk_upsert_recipient_list_members_for_client,
    reconcile_recipient_list_for_client,
)
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


def send_transactional_email_to_recipient_list_for_client(list_key, data, authenticated_client):
    payload = validate_recipient_list_send_payload(data, authenticated_client)
    template = get_transactional_template(authenticated_client, payload["template_key"])
    sender = resolve_sender_email(authenticated_client, payload["from_email"])

    queue_payloads = []
    created_count = 0
    enqueued_count = 0
    skipped_count = 0
    idempotent_replay_count = 0
    member_sync_result = None

    with transaction.atomic():
        if payload["members"] is not None:
            member_sync_result = sync_recipient_list_members_for_send(list_key, payload, authenticated_client)

        recipient_list = (
            RecipientList.objects.select_related("client", "audience")
            .filter(
                client=authenticated_client,
                audience=payload["audience"],
                key=list_key,
            )
            .first()
        )
        if recipient_list is None:
            raise ApiValidationError({"list_key": "not_found"}, status_code=404)

        members = list(recipient_list.members.select_related("contact").filter(active=True).order_by("id"))
        member_contexts = [
            (member, recipient_list_member_context(payload["context"], member.metadata)) for member in members
        ]
        for _, context in member_contexts:
            validate_template_context(template, context)

        for member, context in member_contexts:
            idempotency_key = f"{payload['idempotency_key']}:{member.source_object_key}"
            existing = find_existing_message(authenticated_client, idempotency_key)
            if existing is not None:
                idempotent_replay_count += 1
                continue

            message_payload = {
                "email": member.email,
                "template_key": template.key,
                "idempotency_key": idempotency_key,
                "context": context,
                "metadata": payload["metadata"]
                | {
                    "recipient_list_key": recipient_list.key,
                    "recipient_list_member_id": member.id,
                    "source_object_key": member.source_object_key,
                    "recipient_list_member_metadata": member.metadata,
                    "audience": recipient_list.audience.slug,
                },
                "from_email": payload["from_email"],
            }

            if is_transactional_email_allowed(member.contact):
                message = create_transactional_message(
                    client=authenticated_client,
                    contact=member.contact,
                    template=template,
                    payload=message_payload,
                    sender=sender,
                    idempotency_key=idempotency_key,
                    status=TransactionalMessageStatus.QUEUED,
                )
                append_transactional_event(
                    message,
                    EmailEventType.QUEUED,
                    {
                        "template_key": template.key,
                        "recipient_list_key": recipient_list.key,
                    },
                )
                queue_payloads.append(build_transactional_queue_payload(message))
                created_count += 1
                enqueued_count += 1
                continue

            message = create_transactional_message(
                client=authenticated_client,
                contact=member.contact,
                template=template,
                payload=message_payload,
                sender=sender,
                idempotency_key=idempotency_key,
                status=TransactionalMessageStatus.SKIPPED,
                last_error=suppression_reason(member.contact),
            )
            append_transactional_event(
                message,
                EmailEventType.SKIPPED,
                {
                    "reason": message.last_error,
                    "recipient_list_key": recipient_list.key,
                },
            )
            created_count += 1
            skipped_count += 1

        def enqueue_payloads():
            for queue_payload in queue_payloads:
                enqueue_transactional_email(queue_payload)

        transaction.on_commit(enqueue_payloads)

    response = {
        "recipient_list": {
            "key": recipient_list.key,
            "active_member_count": recipient_list.active_member_count,
        },
        "template_key": template.key,
        "idempotency_key": payload["idempotency_key"],
        "created_count": created_count,
        "enqueued_count": enqueued_count,
        "skipped_count": skipped_count,
        "idempotent_replay_count": idempotent_replay_count,
    }
    if member_sync_result is not None:
        response["member_sync"] = member_sync_result
    return response


def sync_recipient_list_members_for_send(list_key, payload, authenticated_client):
    sync_payload = {
        "audience": payload["audience"].slug,
        "client": payload["client"].slug,
        "members": payload["members"],
    }
    if payload["list"] is not None:
        sync_payload["list"] = payload["list"]
    if payload["member_sync"] == "reconcile":
        sync_payload["remove_absent"] = payload["remove_absent_members"]
        return reconcile_recipient_list_for_client(list_key, sync_payload, authenticated_client)
    return bulk_upsert_recipient_list_members_for_client(list_key, sync_payload, authenticated_client)


def recipient_list_member_context(base_context, member_metadata):
    member = member_metadata if isinstance(member_metadata, dict) else {}
    context = member.copy()
    context.update(base_context)
    context["member"] = member.copy()
    return context


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

    category_tag = data.get("category_tag", "")
    if category_tag in (None, ""):
        category_tag = ""
    elif not isinstance(category_tag, str) or not category_tag.strip():
        errors["category_tag"] = "must_be_non_empty_string"
    else:
        category_tag = category_tag.strip()

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
        "metadata": metadata | ({"category_tag": category_tag} if category_tag else {}),
        "category_tag": category_tag,
        "from_email": from_email,
    }


def validate_recipient_list_send_payload(data, authenticated_client):
    scope = validate_contact_scope(data, authenticated_client, require_email=False)
    errors = {}

    template_key = data.get("template_key")
    if not isinstance(template_key, str) or not template_key.strip():
        errors["template_key"] = "required"

    idempotency_key = data.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        errors["idempotency_key"] = "required"

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

    category_tag = data.get("category_tag", "")
    if category_tag in (None, ""):
        category_tag = ""
    elif not isinstance(category_tag, str) or not category_tag.strip():
        errors["category_tag"] = "must_be_non_empty_string"
    else:
        category_tag = category_tag.strip()

    members = data.get("members")
    if members is None:
        members = None
    elif not isinstance(members, list):
        errors["members"] = "must_be_list"

    member_sync = data.get(
        "member_sync",
        "reconcile" if members is not None else "upsert",
    )
    if member_sync not in {"upsert", "reconcile"}:
        errors["member_sync"] = "invalid"

    remove_absent_members = data.get("remove_absent_members", True)
    if not isinstance(remove_absent_members, bool):
        errors["remove_absent_members"] = "must_be_boolean"

    list_data = data.get("list")
    if list_data is None:
        list_data = None
    elif not isinstance(list_data, dict):
        errors["list"] = "must_be_object"

    from_email = ""
    if "from_email" in data and data.get("from_email") not in (None, ""):
        try:
            from_email = normalize_sender_id(data.get("from_email"))
        except ApiValidationError as exc:
            errors.update(exc.errors)

    if errors:
        raise ApiValidationError(errors)

    return {
        "audience": scope.audience,
        "client": scope.client,
        "template_key": template_key.strip(),
        "idempotency_key": idempotency_key.strip(),
        "context": context,
        "metadata": metadata | ({"category_tag": category_tag} if category_tag else {}),
        "category_tag": category_tag,
        "members": members,
        "member_sync": member_sync,
        "remove_absent_members": remove_absent_members,
        "list": list_data,
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
    event = EmailEvent.objects.create(
        transactional_message=message,
        contact=message.contact,
        client=message.client,
        event_type=event_type,
        metadata=metadata,
    )
    emit_cmp_contact_event(event)
    return event


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
