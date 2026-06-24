import json

import pytest

from mailing.sqs import json_body, process_sqs_event, records_from_messages

pytestmark = pytest.mark.aws_local


def test_localstack_sqs_can_enqueue_and_receive(local_sqs_client, unique_queue_name):
    queue_url = local_sqs_client.create_queue(
        QueueName=unique_queue_name("transactional-email"),
    )["QueueUrl"]

    local_sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({"type": "transactional_email", "message_id": "msg_123"}),
    )

    response = local_sqs_client.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=1,
    )

    event = records_from_messages(response["Messages"])
    processed = []
    batch_response = process_sqs_event(event, lambda body, record: processed.append(body))

    assert json_body(event["Records"][0]) == {"type": "transactional_email", "message_id": "msg_123"}
    assert processed == [{"type": "transactional_email", "message_id": "msg_123"}]
    assert batch_response == {"batchItemFailures": []}


def test_localstack_sqs_can_attach_dead_letter_queue(local_sqs_client, unique_queue_name):
    dlq_url = local_sqs_client.create_queue(QueueName=unique_queue_name("transactional-email-dlq"))["QueueUrl"]
    dlq_arn = local_sqs_client.get_queue_attributes(
        QueueUrl=dlq_url,
        AttributeNames=["QueueArn"],
    )["Attributes"]["QueueArn"]

    queue_url = local_sqs_client.create_queue(
        QueueName=unique_queue_name("transactional-email"),
        Attributes={
            "RedrivePolicy": json.dumps({
                "deadLetterTargetArn": dlq_arn,
                "maxReceiveCount": "3",
            })
        },
    )["QueueUrl"]

    attributes = local_sqs_client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["RedrivePolicy"],
    )["Attributes"]

    assert json.loads(attributes["RedrivePolicy"])["deadLetterTargetArn"] == dlq_arn
