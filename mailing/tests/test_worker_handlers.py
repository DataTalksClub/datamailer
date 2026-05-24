import json

import pytest

from mailing.sqs import records_from_messages
from mailing.workers import (
    campaign_email_handler,
    email_events_handler,
    ses_webhooks_handler,
    transactional_email_handler,
)

VALID_MESSAGES = {
    "transactional": {
        "handler": transactional_email_handler,
        "payload": {
            "contract": "transactional-email",
            "version": 1,
            "transactional_message_id": 101,
            "client_id": 7,
            "contact_id": 22,
            "template_id": 4,
            "template_key": "password-reset",
            "idempotency_key": "client-7:password-reset:req-123",
            "metadata": {"trace_id": "trace-1"},
        },
    },
    "campaign": {
        "handler": campaign_email_handler,
        "payload": {
            "contract": "campaign-email",
            "version": 1,
            "campaign_id": 55,
            "batch_id": "campaign-55-batch-0001",
            "campaign_recipient_ids": [501, 502],
            "idempotency_key": "campaign-55-batch-0001",
            "metadata": {"source": "scheduler"},
        },
    },
    "ses_webhook": {
        "handler": ses_webhooks_handler,
        "payload": {
            "contract": "ses-webhooks",
            "version": 1,
            "provider": "ses",
            "provider_event_id": "sns-message-123",
            "notification_type": "bounce",
            "received_at": "2026-05-24T12:00:00Z",
            "ses_message_id": "ses-message-123",
            "metadata": {"mail_timestamp": "2026-05-24T11:59:59Z"},
        },
    },
    "email_events": {
        "handler": email_events_handler,
        "payload": {
            "contract": "email-events",
            "version": 1,
            "event_id": "track-open-123",
            "event_type": "open",
            "occurred_at": "2026-05-24T12:00:01Z",
            "idempotency_key": "open:tracking-token-123:2026-05-24T12:00:01Z",
            "campaign_recipient_id": 501,
            "tracking_token": "tracking-token-123",
            "metadata": {"user_agent": "pytest"},
        },
    },
}


@pytest.mark.parametrize("worker", VALID_MESSAGES.values(), ids=VALID_MESSAGES.keys())
def test_worker_handler_accepts_valid_sqs_event(worker):
    event = _event_from_payloads([("message-1", worker["payload"])])

    assert worker["handler"](event, None) == {"batchItemFailures": []}


@pytest.mark.parametrize("worker", VALID_MESSAGES.values(), ids=VALID_MESSAGES.keys())
def test_worker_handler_rejects_invalid_sqs_record(worker):
    invalid_payload = worker["payload"] | {"version": 999}
    event = _event_from_payloads([("message-1", invalid_payload)])

    assert worker["handler"](event, None) == {
        "batchItemFailures": [{"itemIdentifier": "message-1"}],
    }


@pytest.mark.parametrize("worker", VALID_MESSAGES.values(), ids=VALID_MESSAGES.keys())
def test_worker_handler_returns_partial_batch_failures(worker):
    invalid_payload = worker["payload"].copy()
    invalid_payload.pop("contract")
    event = _event_from_payloads(
        [
            ("valid-message", worker["payload"]),
            ("invalid-message", invalid_payload),
        ]
    )

    assert worker["handler"](event, None) == {
        "batchItemFailures": [{"itemIdentifier": "invalid-message"}],
    }


@pytest.mark.parametrize("worker", VALID_MESSAGES.values(), ids=VALID_MESSAGES.keys())
def test_worker_handler_reports_malformed_json_as_record_failure(worker):
    event = records_from_messages(
        [
            {
                "MessageId": "bad-json",
                "ReceiptHandle": "receipt-1",
                "Body": "{not-json",
            }
        ]
    )

    assert worker["handler"](event, None) == {
        "batchItemFailures": [{"itemIdentifier": "bad-json"}],
    }


def test_campaign_handler_requires_recipient_ids_for_idempotent_row_level_sends():
    payload = VALID_MESSAGES["campaign"]["payload"] | {"campaign_recipient_ids": []}
    event = _event_from_payloads([("message-1", payload)])

    assert campaign_email_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "message-1"}],
    }


def _event_from_payloads(message_payloads):
    return records_from_messages(
        [
            {
                "MessageId": message_id,
                "ReceiptHandle": f"{message_id}-receipt",
                "Body": json.dumps(payload),
            }
            for message_id, payload in message_payloads
        ]
    )
