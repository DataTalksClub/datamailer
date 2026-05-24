from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CONTRACT_VERSION = 1

TRANSACTIONAL_EMAIL_CONTRACT = "transactional-email"
CAMPAIGN_EMAIL_CONTRACT = "campaign-email"
SES_WEBHOOKS_CONTRACT = "ses-webhooks"
EMAIL_EVENTS_CONTRACT = "email-events"

TRACKING_EVENT_TYPES = {"open", "click", "unsubscribe"}
SES_NOTIFICATION_TYPES = {"send", "reject", "delivery", "bounce", "complaint", "open", "click"}


class ContractValidationError(ValueError):
    """Raised when an SQS message does not match the queue contract."""


@dataclass(frozen=True)
class QueueContract:
    name: str
    required_fields: dict[str, Callable[[Any], bool]]
    optional_fields: dict[str, Callable[[Any], bool]]


def validate_transactional_email_message(payload):
    return _validate_payload(
        payload,
        QueueContract(
            name=TRANSACTIONAL_EMAIL_CONTRACT,
            required_fields={
                "transactional_message_id": _positive_int,
                "client_id": _positive_int,
                "idempotency_key": _non_empty_str,
            },
            optional_fields={
                "contact_id": _positive_int,
                "template_id": _positive_int,
                "template_key": _non_empty_str,
                "metadata": _dict,
            },
        ),
    )


def validate_campaign_email_message(payload):
    return _validate_payload(
        payload,
        QueueContract(
            name=CAMPAIGN_EMAIL_CONTRACT,
            required_fields={
                "campaign_id": _positive_int,
                "batch_id": _non_empty_str,
                "campaign_recipient_ids": _non_empty_positive_int_list,
            },
            optional_fields={
                "idempotency_key": _non_empty_str,
                "metadata": _dict,
            },
        ),
    )


def validate_ses_webhook_message(payload):
    return _validate_payload(
        payload,
        QueueContract(
            name=SES_WEBHOOKS_CONTRACT,
            required_fields={
                "provider": _ses_provider,
                "provider_event_id": _non_empty_str,
                "notification_type": lambda value: value in SES_NOTIFICATION_TYPES,
                "received_at": _non_empty_str,
            },
            optional_fields={
                "ses_message_id": _non_empty_str,
                "mail_message_id": _non_empty_str,
                "raw_payload_s3_key": _non_empty_str,
                "metadata": _dict,
            },
        ),
    )


def validate_email_event_message(payload):
    return _validate_payload(
        payload,
        QueueContract(
            name=EMAIL_EVENTS_CONTRACT,
            required_fields={
                "event_id": _non_empty_str,
                "event_type": lambda value: value in TRACKING_EVENT_TYPES,
                "occurred_at": _non_empty_str,
                "idempotency_key": _non_empty_str,
            },
            optional_fields={
                "campaign_id": _positive_int,
                "campaign_recipient_id": _positive_int,
                "transactional_message_id": _positive_int,
                "contact_id": _positive_int,
                "client_id": _positive_int,
                "audience_id": _positive_int,
                "tracking_token": _non_empty_str,
                "url": _non_empty_str,
                "metadata": _dict,
            },
        ),
    )


def _validate_payload(payload, contract):
    if not isinstance(payload, dict):
        raise ContractValidationError("message body must be a JSON object")

    if payload.get("contract") != contract.name:
        raise ContractValidationError(f"contract must be {contract.name!r}")

    if payload.get("version") != CONTRACT_VERSION:
        raise ContractValidationError(f"version must be {CONTRACT_VERSION}")

    for field_name, validator in contract.required_fields.items():
        if field_name not in payload:
            raise ContractValidationError(f"missing required field {field_name!r}")
        if not validator(payload[field_name]):
            raise ContractValidationError(f"invalid field {field_name!r}")

    for field_name, validator in contract.optional_fields.items():
        if field_name in payload and not validator(payload[field_name]):
            raise ContractValidationError(f"invalid field {field_name!r}")

    return payload


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_empty_str(value):
    return isinstance(value, str) and bool(value.strip())


def _dict(value):
    return isinstance(value, dict)


def _non_empty_positive_int_list(value):
    return isinstance(value, list) and bool(value) and all(_positive_int(item) for item in value)


def _ses_provider(value):
    return value == "ses"
