from mailing.queue_contracts import (
    validate_campaign_email_message,
    validate_email_event_message,
    validate_transactional_email_message,
)
from mailing.services.campaign_sender import send_campaign_batch
from mailing.services.ses_webhooks import normalize_ses_webhook_worker_payload, process_ses_webhook
from mailing.services.transactional_sender import send_transactional_email_from_queue
from mailing.sqs import process_sqs_event


def transactional_email_handler(event, context=None):
    return process_sqs_event(event, _handle_transactional_email_record)


def campaign_email_handler(event, context=None):
    return process_sqs_event(event, _handle_campaign_email_record)


def ses_webhooks_handler(event, context=None):
    return process_sqs_event(event, _handle_ses_webhook_record)


def email_events_handler(event, context=None):
    return process_sqs_event(event, _handle_email_event_record)


def _handle_transactional_email_record(payload, record):
    validate_transactional_email_message(payload)
    send_transactional_email_from_queue(payload)


def _handle_campaign_email_record(payload, record):
    validate_campaign_email_message(payload)
    send_campaign_batch(payload)


def _handle_ses_webhook_record(payload, record):
    process_ses_webhook(normalize_ses_webhook_worker_payload(payload))


def _handle_email_event_record(payload, record):
    validate_email_event_message(payload)
    # Future event ingest uses idempotency_key for edge-generated tracking
    # events while keeping email_events append-only for auditable history.
