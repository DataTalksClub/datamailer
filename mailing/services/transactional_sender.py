from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mailing.aws import ses_client
from mailing.models import EmailEvent, EmailEventType, TransactionalMessage, TransactionalMessageStatus
from mailing.services.cmp_callbacks import emit_cmp_contact_event
from mailing.services.mock_inbox import is_mock_address
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
        with transaction.atomic():
            message = (
                TransactionalMessage.objects.select_for_update().select_related("client", "contact").get(id=message_id)
            )

            if message.status in TERMINAL_ACK_STATUSES:
                return message

            if message.client_id != payload["client_id"] or message.idempotency_key != payload["idempotency_key"]:
                _mark_failed(
                    message,
                    "queue payload does not match transactional message client_id/idempotency_key",
                    metadata={
                        "reason": "queue_payload_mismatch",
                        "queued_client_id": payload["client_id"],
                        "queued_idempotency_key": payload["idempotency_key"],
                    },
                )
                return message

            if message.status != TransactionalMessageStatus.QUEUED:
                _record_retryable_error(message.id, f"transactional message status is not sendable: {message.status}")
                raise TransientSendFailure(f"transactional message status is not sendable: {message.status}")

            if is_mock_address(message.email):
                sent_at = timezone.now()
                message.status = TransactionalMessageStatus.SENT
                message.ses_message_id = f"mock-inbox:{message.id}"
                message.sent_at = sent_at
                message.last_error = ""
                message.save(update_fields=["status", "ses_message_id", "sent_at", "last_error", "updated_at"])
                _append_event(
                    message,
                    EmailEventType.SENT,
                    {"captured": True, "mock_inbox": True, "sent_at": sent_at.isoformat()},
                )
                return message

            source = source or message.from_email or message.client.default_from_email or settings.DEFAULT_FROM_EMAIL
            try:
                ses_message_id = send_email(
                    ses_client=client or ses_client(),
                    source=source,
                    to_email=message.email,
                    subject=message.subject,
                    html_body=message.html_body,
                    text_body=message.text_body,
                )
            except ClientError as exc:
                if _is_permanent_client_error(exc):
                    _mark_failed(message, _error_message(exc), metadata={"reason": "ses_permanent_failure"})
                    return message
                raise TransientSendFailure(_error_message(exc)) from exc
            except BotoCoreError as exc:
                raise TransientSendFailure(_error_message(exc)) from exc

            sent_at = timezone.now()
            message.status = TransactionalMessageStatus.SENT
            message.ses_message_id = ses_message_id
            message.sent_at = sent_at
            message.last_error = ""
            message.save(update_fields=["status", "ses_message_id", "sent_at", "last_error", "updated_at"])
            _append_event(
                message,
                EmailEventType.SENT,
                {"ses_message_id": ses_message_id, "sent_at": sent_at.isoformat()},
            )
            return message
    except TransactionalMessage.DoesNotExist as exc:
        raise TransactionalMessageNotFound(f"transactional message {message_id} was not found") from exc
    except TransientSendFailure as exc:
        _record_retryable_error(message_id, str(exc))
        raise


def _is_permanent_client_error(exc):
    return exc.response.get("Error", {}).get("Code", "") in PERMANENT_SES_ERROR_CODES


def _mark_failed(message, error, *, metadata):
    message.status = TransactionalMessageStatus.FAILED
    message.last_error = error
    message.save(update_fields=["status", "last_error", "updated_at"])
    _append_event(message, EmailEventType.FAILED, metadata | {"error": error})


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
