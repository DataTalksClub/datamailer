import json

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from mailing.aws import sqs_client


def partial_batch_response(failed_message_ids):
    return {
        "batchItemFailures": [
            {"itemIdentifier": message_id}
            for message_id in failed_message_ids
        ]
    }


def records_from_messages(messages, *, event_source_arn="arn:aws:sqs:us-east-1:000000000000:test"):
    return {
        "Records": [
            {
                "messageId": message["MessageId"],
                "receiptHandle": message["ReceiptHandle"],
                "body": message["Body"],
                "attributes": message.get("Attributes", {}),
                "messageAttributes": message.get("MessageAttributes", {}),
                "md5OfBody": message.get("MD5OfBody", ""),
                "eventSource": "aws:sqs",
                "eventSourceARN": event_source_arn,
                "awsRegion": "us-east-1",
            }
            for message in messages
        ]
    }


def json_body(record):
    return json.loads(record["body"])


def process_sqs_event(event, handler):
    failed_message_ids = []
    for record in event.get("Records", []):
        try:
            handler(json_body(record), record)
        except Exception:
            failed_message_ids.append(record["messageId"])

    return partial_batch_response(failed_message_ids)


def send_sqs_json_message(*, queue_url, payload, client=None):
    if not queue_url:
        raise ImproperlyConfigured("SQS queue URL is required.")

    sqs = client or sqs_client()
    return sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(payload, sort_keys=True),
    )


def enqueue_transactional_email(payload, *, client=None):
    return send_sqs_json_message(
        queue_url=settings.SQS_TRANSACTIONAL_EMAIL_QUEUE_URL,
        payload=payload,
        client=client,
    )


def enqueue_campaign_email(payload, *, client=None):
    return send_sqs_json_message(
        queue_url=settings.SQS_CAMPAIGN_EMAIL_QUEUE_URL,
        payload=payload,
        client=client,
    )


def enqueue_ses_webhook(payload, *, client=None):
    return send_sqs_json_message(
        queue_url=settings.SQS_SES_WEBHOOKS_QUEUE_URL,
        payload=payload,
        client=client,
    )
