import json

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command

from mailing.sqs_worker import SqsWorker, WorkerConfig, get_worker_config


class FakeSqsClient:
    def __init__(self, messages):
        self.messages = messages
        self.receive_calls = []
        self.deleted_receipts = []

    def receive_message(self, **kwargs):
        self.receive_calls.append(kwargs)
        return {"Messages": self.messages}

    def delete_message(self, **kwargs):
        self.deleted_receipts.append(kwargs["ReceiptHandle"])


def test_sqs_worker_deletes_successful_messages_only():
    messages = [
        _message("success-1"),
        _message("failed-1"),
        _message("success-2"),
    ]
    client = FakeSqsClient(messages)

    def handler(event, context):
        assert [record["messageId"] for record in event["Records"]] == ["success-1", "failed-1", "success-2"]
        return {"batchItemFailures": [{"itemIdentifier": "failed-1"}]}

    worker = SqsWorker(
        WorkerConfig(name="test", queue_url="https://sqs.example/test", handler=handler),
        client=client,
        batch_size=3,
        wait_time=7,
        visibility_timeout=30,
    )

    result = worker.run_once()

    assert result.received == 3
    assert result.deleted == 2
    assert result.failed == 1
    assert client.deleted_receipts == ["success-1-receipt", "success-2-receipt"]
    assert client.receive_calls == [
        {
            "QueueUrl": "https://sqs.example/test",
            "MaxNumberOfMessages": 3,
            "WaitTimeSeconds": 7,
            "MessageAttributeNames": ["All"],
            "AttributeNames": ["All"],
            "VisibilityTimeout": 30,
        }
    ]


def test_sqs_worker_empty_poll_does_not_delete():
    client = FakeSqsClient([])
    worker = SqsWorker(
        WorkerConfig(name="test", queue_url="https://sqs.example/test", handler=lambda event, context: None),
        client=client,
    )

    result = worker.run_once()

    assert result.received == 0
    assert result.deleted == 0
    assert result.failed == 0
    assert client.deleted_receipts == []


def test_get_worker_config_requires_queue_url(settings):
    settings.SQS_TRANSACTIONAL_EMAIL_QUEUE_URL = ""

    with pytest.raises(ImproperlyConfigured, match="transactional worker"):
        get_worker_config("transactional")


def test_process_sqs_worker_once(settings, monkeypatch):
    settings.SQS_TRANSACTIONAL_EMAIL_QUEUE_URL = "https://sqs.example/transactional"
    client = FakeSqsClient([_message("message-1")])

    monkeypatch.setattr("mailing.sqs_worker.sqs_client", lambda: client)
    monkeypatch.setattr(
        "mailing.sqs_worker.transactional_email_handler",
        lambda event, context: {"batchItemFailures": []},
    )

    call_command("process_sqs_worker", "transactional", "--once")

    assert client.deleted_receipts == ["message-1-receipt"]


def _message(message_id, payload=None):
    return {
        "MessageId": message_id,
        "ReceiptHandle": f"{message_id}-receipt",
        "Body": json.dumps(payload or {"contract": "test"}),
        "MD5OfBody": "md5",
    }
