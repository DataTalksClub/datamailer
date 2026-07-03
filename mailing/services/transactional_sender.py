from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mailing.aws import ses_client
from mailing.models import EmailEvent, EmailEventType, TransactionalMessage, TransactionalMessageStatus
from mailing.services.cmp_callbacks import emit_cmp_contact_event
from mailing.ses import send_email

TERMINAL_ACK_STATUSES = {
    TransactionalMessageStatus.SENT,
    TransactionalMessageStatus.SKIPPED,
    TransactionalMessageStatus.FAILED,
    TransactionalMessageStatus.BOUNCED,
    TransactionalMessageStatus.COMPLAINED,
}

PERMANENT_SES_ERROR_CODES = {
    "ConfigurationSetDoesNotExistException",
    "InvalidParameterCombination",
    "InvalidParameterValue",
    "InvalidParameterValueException",
    "MailFromDomainNotVerifiedException",
    "MessageRejected",
    "ValidationError",
}


class TransactionalMessageNotFound(RuntimeError):
    """Raised for valid queue records whose source-of-truth row is unavailable."""


class TransientSendFailure(RuntimeError):
    """Raised when Lambda should return this SQS record for retry."""


def send_transactional_email_from_queue(payload, *, client=None, source=None):
    message_id = payload["transactional_message_id"]

    try:
        message = _claim_message_for_send(payload)

        if message.status in TERMINAL_ACK_STATUSES:
            return message

        if message.status == TransactionalMessageStatus.SENDING and not getattr(message, "_claimed_for_send", False):
            raise TransientSendFailure(f"transactional message {message.id} is already sending")

        source = source or message.from_email or message.client.default_from_email or settings.DEFAULT_FROM_EMAIL
        try:
            ses_message_id = send_email(
                ses_client=client or ses_client(),
                source=source,
                to_email=message.email,
                subject=message.subject,
                html_body=message.html_body,
                text_body=message.text_body,
                reply_to=message.metadata.get("reply_to", ""),
                cc=message.metadata.get("cc", []),
                bcc=message.metadata.get("bcc", []),
                headers=message.metadata.get("headers", {}),
                message_parts=message.metadata.get("message_parts", []),
            )
        except ClientError as exc:
            if _is_permanent_client_error(exc):
                return _mark_failed(
                    message.id,
                    _error_message(exc),
                    metadata={"reason": "ses_permanent_failure"},
                )
            _mark_retryable(message.id, _error_message(exc))
            raise TransientSendFailure(_error_message(exc)) from exc
        except BotoCoreError as exc:
            _mark_retryable(message.id, _error_message(exc))
            raise TransientSendFailure(_error_message(exc)) from exc

        sent_at = timezone.now()
        return _mark_sent(
            message.id,
            ses_message_id,
            {"ses_message_id": ses_message_id, "sent_at": sent_at.isoformat()},
        )
    except TransactionalMessage.DoesNotExist as exc:
        raise TransactionalMessageNotFound(f"transactional message {message_id} was not found") from exc
    except TransientSendFailure as exc:
        _record_retryable_error(message_id, str(exc))
        raise


def _is_permanent_client_error(exc):
    return exc.response.get("Error", {}).get("Code", "") in PERMANENT_SES_ERROR_CODES


def _claim_message_for_send(payload):
    with transaction.atomic():
        message = (
            TransactionalMessage.objects.select_for_update()
            .select_related("client", "contact")
            .get(id=payload["transactional_message_id"])
        )

        if message.status in TERMINAL_ACK_STATUSES:
            return message

        if message.client_id != payload["client_id"] or message.idempotency_key != payload["idempotency_key"]:
            return _mark_failed(
                message.id,
                "queue payload does not match transactional message client_id/idempotency_key",
                metadata={
                    "reason": "queue_payload_mismatch",
                    "queued_client_id": payload["client_id"],
                    "queued_idempotency_key": payload["idempotency_key"],
                },
            )

        if message.status == TransactionalMessageStatus.SENDING:
            return message

        if message.status != TransactionalMessageStatus.QUEUED:
            _record_retryable_error(
                message.id,
                f"transactional message status is not sendable: {message.status}",
            )
            raise TransientSendFailure(f"transactional message status is not sendable: {message.status}")

        message.status = TransactionalMessageStatus.SENDING
        message.last_error = ""
        message.save(update_fields=["status", "last_error", "updated_at"])
        message._claimed_for_send = True
        return message


def _mark_sent(message_id, ses_message_id, metadata):
    with transaction.atomic():
        message = (
            TransactionalMessage.objects.select_for_update().select_related("client", "contact").get(id=message_id)
        )
        if message.status in TERMINAL_ACK_STATUSES:
            return message

        sent_at = timezone.now()
        message.status = TransactionalMessageStatus.SENT
        message.ses_message_id = ses_message_id
        message.sent_at = sent_at
        message.last_error = ""
        message.save(update_fields=["status", "ses_message_id", "sent_at", "last_error", "updated_at"])
        _append_event(message, EmailEventType.SENT, metadata)
        return message


def _mark_failed(message_id, error, *, metadata):
    with transaction.atomic():
        message = (
            TransactionalMessage.objects.select_for_update().select_related("client", "contact").get(id=message_id)
        )
        if message.status in TERMINAL_ACK_STATUSES:
            return message

        message.status = TransactionalMessageStatus.FAILED
        message.last_error = error
        message.save(update_fields=["status", "last_error", "updated_at"])
        _append_event(message, EmailEventType.FAILED, metadata | {"error": error})
        return message


def _mark_retryable(message_id, error):
    TransactionalMessage.objects.filter(
        id=message_id,
        status=TransactionalMessageStatus.SENDING,
    ).update(
        status=TransactionalMessageStatus.QUEUED,
        last_error=error,
        updated_at=timezone.now(),
    )


def _record_retryable_error(message_id, error):
    TransactionalMessage.objects.filter(id=message_id, status=TransactionalMessageStatus.QUEUED).update(
        last_error=error,
        updated_at=timezone.now(),
    )


def _append_event(message, event_type, metadata):
    event = EmailEvent.objects.create(
        transactional_message=message,
        contact=message.contact,
        client=message.client,
        event_type=event_type,
        metadata=metadata,
    )
    emit_cmp_contact_event(event)
    return event


def _error_message(exc):
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = error.get("Code", "ClientError")
        message = error.get("Message", str(exc))
        return f"{code}: {message}"
    return str(exc)
