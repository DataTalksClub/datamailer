from mailing.sqs import json_body, partial_batch_response, process_sqs_event, records_from_messages


def test_partial_batch_response_uses_lambda_shape():
    response = partial_batch_response(["message-1", "message-2"])

    assert response == {
        "batchItemFailures": [
            {"itemIdentifier": "message-1"},
            {"itemIdentifier": "message-2"},
        ]
    }


def test_records_from_messages_builds_sqs_event_shape():
    event = records_from_messages(
        [
            {
                "MessageId": "message-1",
                "ReceiptHandle": "receipt-1",
                "Body": '{"type": "transactional"}',
                "MD5OfBody": "md5",
            }
        ]
    )

    assert event["Records"][0]["messageId"] == "message-1"
    assert event["Records"][0]["eventSource"] == "aws:sqs"
    assert json_body(event["Records"][0]) == {"type": "transactional"}


def test_process_sqs_event_passes_decoded_messages_to_handler():
    event = records_from_messages(
        [
            {
                "MessageId": "message-1",
                "ReceiptHandle": "receipt-1",
                "Body": '{"type": "transactional"}',
            }
        ]
    )
    processed = []

    response = process_sqs_event(event, lambda body, record: processed.append((body, record["messageId"])))

    assert processed == [({"type": "transactional"}, "message-1")]
    assert response == {"batchItemFailures": []}


def test_process_sqs_event_reports_failed_message_ids():
    event = records_from_messages(
        [
            {
                "MessageId": "message-1",
                "ReceiptHandle": "receipt-1",
                "Body": '{"type": "transactional"}',
            }
        ]
    )

    def handler(body, record):
        raise RuntimeError("transient failure")

    assert process_sqs_event(event, handler) == {
        "batchItemFailures": [{"itemIdentifier": "message-1"}],
    }
