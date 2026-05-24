import json


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
