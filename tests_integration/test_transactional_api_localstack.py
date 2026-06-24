import json

import pytest
from django.urls import reverse
from django.utils import timezone

from mailing.models import Client, Contact, EmailTemplate, Organization, TransactionalMessage
from mailing.queue_contracts import validate_transactional_email_message
from mailing.services.auth import create_client_api_key
from mailing.sqs import json_body, records_from_messages

pytestmark = [pytest.mark.aws_local, pytest.mark.django_db(transaction=True)]

API_KEY = "test-client-key"


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def api_client_record(organization):
    client = Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
        default_sender_id="newsletter",
        sender_emails=[{"id": "newsletter", "email": "newsletter@example.com"}],
    )
    create_client_api_key(client=client, name="Transactional test", raw_api_key=API_KEY)
    return client


@pytest.fixture
def template(api_client_record):
    return EmailTemplate.objects.create(
        client=api_client_record,
        key="email-verification",
        name="Email verification",
        subject="Verify {{ product }}",
        html_body="<p>Verify at {{ verification_url }}</p>",
        text_body="Verify at {{ verification_url }}",
    )


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def post_transactional(django_client, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_transactional_send"),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def test_transactional_endpoint_enqueues_to_localstack_sqs(
    client,
    template,
    local_sqs_client,
    unique_queue_name,
    settings,
):
    queue_url = local_sqs_client.create_queue(QueueName=unique_queue_name("transactional-email"))["QueueUrl"]
    settings.SQS_TRANSACTIONAL_EMAIL_QUEUE_URL = queue_url

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "idempotency_key": "localstack-1",
            "metadata": {"trace_id": "trace-1"},
        },
    )

    sqs_response = local_sqs_client.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=1,
    )
    event = records_from_messages(sqs_response["Messages"])
    body = json_body(event["Records"][0])

    assert response.status_code == 202
    assert validate_transactional_email_message(body) == body
    assert body["transactional_message_id"] == TransactionalMessage.objects.get().id
    assert body["metadata"] == {"trace_id": "trace-1"}


def test_transactional_duplicates_and_suppression_do_not_enqueue_to_localstack_sqs(
    client,
    template,
    local_sqs_client,
    unique_queue_name,
    settings,
):
    queue_url = local_sqs_client.create_queue(QueueName=unique_queue_name("transactional-email"))["QueueUrl"]
    settings.SQS_TRANSACTIONAL_EMAIL_QUEUE_URL = queue_url
    payload = {
        "email": "person@example.com",
        "template_key": template.key,
        "idempotency_key": "localstack-duplicate",
    }

    assert post_transactional(client, payload).status_code == 202
    assert post_transactional(client, payload).status_code == 202

    Contact.objects.create(email="blocked@example.com", hard_bounced_at=timezone.now())
    suppressed = post_transactional(
        client,
        {
            "email": "blocked@example.com",
            "template_key": template.key,
            "idempotency_key": "localstack-suppressed",
        },
    )

    messages = local_sqs_client.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=1,
    )["Messages"]

    assert suppressed.status_code == 409
    assert [json.loads(message["Body"])["idempotency_key"] for message in messages] == ["localstack-duplicate"]
