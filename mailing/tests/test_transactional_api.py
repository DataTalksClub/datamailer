import json

import pytest
from django.contrib import admin
from django.urls import reverse
from django.utils import timezone

from mailing.admin import EmailEventAdmin, EmailTemplateAdmin, TransactionalMessageAdmin
from mailing.models import (
    Audience,
    Client,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    Subscription,
    SubscriptionStatus,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.queue_contracts import validate_transactional_email_message
from mailing.services.auth import hash_api_key
from mailing.sqs import json_body, records_from_messages

pytestmark = pytest.mark.django_db(transaction=True)

API_KEY = "test-client-key"


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def api_client_record(organization):
    return Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
        api_key_hash=hash_api_key(API_KEY),
    )


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def other_client(organization):
    return Client.objects.create(
        organization=organization,
        name="DTC Newsletter",
        slug="dtc-newsletter",
        api_key_hash=hash_api_key("other-key"),
    )


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


def test_transactional_send_creates_message_event_and_contract_queue_payload(
    client,
    template,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_transactional(
        client,
        {
            "email": " Person@Example.COM ",
            "template_key": template.key,
            "idempotency_key": "verify-123",
            "context": {
                "product": "Datamailer",
                "verification_url": "https://example.com/verify/token",
            },
            "metadata": {"user_id": "42"},
        },
    )

    message = TransactionalMessage.objects.get()
    contact = Contact.objects.get()
    event = EmailEvent.objects.get()

    assert response.status_code == 202
    assert response.json()["message"]["id"] == message.id
    assert response.json()["message"]["status"] == TransactionalMessageStatus.QUEUED
    assert response.json()["idempotent_replay"] is False
    assert response.json()["enqueued"] is True
    assert contact.normalized_email == "person@example.com"
    assert message.email == "person@example.com"
    assert message.template == template
    assert message.template_key == template.key
    assert message.subject == "Verify Datamailer"
    assert message.html_body == "<p>Verify at https://example.com/verify/token</p>"
    assert message.text_body == "Verify at https://example.com/verify/token"
    assert message.context["verification_url"] == "https://example.com/verify/token"
    assert message.metadata == {"user_id": "42"}
    assert event.event_type == EmailEventType.QUEUED
    assert event.transactional_message == message
    assert len(enqueued) == 1
    assert validate_transactional_email_message(enqueued[0]) == enqueued[0]
    assert enqueued[0]["contract"] == "transactional-email"
    assert enqueued[0]["transactional_message_id"] == message.id
    assert enqueued[0]["client_id"] == template.client_id
    assert enqueued[0]["contact_id"] == contact.id
    assert enqueued[0]["template_id"] == template.id
    assert enqueued[0]["template_key"] == template.key
    assert enqueued[0]["idempotency_key"] == "verify-123"


def test_transactional_idempotency_reuses_existing_message_without_enqueue(client, template, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    payload = {
        "email": "person@example.com",
        "template_key": template.key,
        "idempotency_key": "same-request",
        "context": {"product": "Datamailer"},
    }

    first = post_transactional(client, payload)
    second = post_transactional(client, payload | {"metadata": {"ignored": True}})

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["message"]["id"] == first.json()["message"]["id"]
    assert second.json()["idempotent_replay"] is True
    assert second.json()["enqueued"] is False
    assert TransactionalMessage.objects.count() == 1
    assert EmailEvent.objects.count() == 1
    assert len(enqueued) == 1


def test_transactional_required_context_rejects_before_mutation_or_enqueue(client, api_client_record, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    template = EmailTemplate.objects.create(
        client=api_client_record,
        key="password-reset",
        name="Password Reset",
        subject="Reset",
        text_body="Reset at {{ reset_url }}",
        required_context=[{"name": "reset_url", "description": "Client-generated reset URL."}],
        example_context={"reset_url": "https://client.example/reset/placeholder"},
    )

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "idempotency_key": "missing-context",
            "context": {},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"] == {"context.reset_url": "required"}
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


def test_transactional_idempotent_replay_does_not_revalidate_required_context(client, api_client_record, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    template = EmailTemplate.objects.create(
        client=api_client_record,
        key="email-verification",
        name="Email verification",
        subject="Verify",
        required_context=["verification_url"],
    )
    payload = {
        "email": "person@example.com",
        "template_key": template.key,
        "idempotency_key": "verify-replay",
        "context": {"verification_url": "https://client.example/verify/placeholder"},
    }

    first = post_transactional(client, payload)
    second = post_transactional(client, payload | {"context": {}})

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["idempotent_replay"] is True
    assert TransactionalMessage.objects.count() == 1
    assert len(enqueued) == 1


def test_transactional_send_does_not_require_verified_or_marketing_subscribed_contact(
    client,
    audience,
    template,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    contact = Contact.objects.create(email="person@example.com", global_unsubscribed_at=timezone.now())
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=template.client,
        status=SubscriptionStatus.UNSUBSCRIBED,
    )

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "context": {"product": "Datamailer"},
        },
    )

    assert response.status_code == 202
    assert TransactionalMessage.objects.get().status == TransactionalMessageStatus.QUEUED
    assert len(enqueued) == 1


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("hard_bounced_at", timezone.now(), "hard_bounce"),
        ("complained_at", timezone.now(), "complaint"),
    ],
)
def test_hard_suppressed_transactional_send_creates_skipped_audit_without_enqueue(
    client,
    template,
    monkeypatch,
    field,
    value,
    reason,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    Contact.objects.create(email="person@example.com", **{field: value})

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": template.key,
            "idempotency_key": f"suppressed-{reason}",
        },
    )

    message = TransactionalMessage.objects.get()
    event = EmailEvent.objects.get()
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "transactional_suppressed"
    assert response.json()["error"]["reason"] == reason
    assert message.status == TransactionalMessageStatus.SKIPPED
    assert message.last_error == reason
    assert event.event_type == EmailEventType.SKIPPED
    assert event.metadata == {"reason": reason}
    assert enqueued == []


def test_transactional_template_key_is_scoped_to_authenticated_client(
    client,
    other_client,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    EmailTemplate.objects.create(
        client=other_client,
        key="password-reset",
        name="Other client reset",
        subject="Reset",
    )
    own_template = EmailTemplate.objects.create(
        client=api_client_record,
        key="password-reset",
        name="Own reset",
        subject="Reset {{ product }}",
    )

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": "password-reset",
            "idempotency_key": "reset-1",
            "context": {"product": "Datamailer"},
        },
    )

    assert response.status_code == 202
    message = TransactionalMessage.objects.get()
    assert message.template == own_template
    assert message.client == api_client_record
    assert enqueued[0]["template_id"] == own_template.id


def test_missing_or_campaign_only_template_returns_clear_404_without_mutation(
    client,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    EmailTemplate.objects.create(
        client=api_client_record,
        key="campaign-layout",
        name="Campaign layout",
        subject="News",
        is_transactional=False,
    )

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": "campaign-layout",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"template_key": "not_found"}
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


def test_inactive_transactional_template_returns_clear_404_without_mutation(
    client,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)
    EmailTemplate.objects.create(
        client=api_client_record,
        key="inactive",
        name="Inactive",
        subject="Inactive",
        is_active=False,
    )

    response = post_transactional(
        client,
        {
            "email": "person@example.com",
            "template_key": "inactive",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["fields"] == {"template_key": "not_found"}
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"template_key": "email-verification"}, "email"),
        ({"email": "not-email", "template_key": "email-verification"}, "email"),
        ({"email": "person@example.com"}, "template_key"),
        ({"email": "person@example.com", "template_key": "email-verification", "context": []}, "context"),
        ({"email": "person@example.com", "template_key": "email-verification", "metadata": []}, "metadata"),
        (
            {"email": "person@example.com", "template_key": "email-verification", "idempotency_key": []},
            "idempotency_key",
        ),
    ],
)
def test_transactional_validation_errors_do_not_mutate(client, template, payload, field, monkeypatch):
    enqueued = []
    monkeypatch.setattr("mailing.services.transactional.enqueue_transactional_email", enqueued.append)

    response = post_transactional(client, payload)

    assert response.status_code == 400
    assert field in response.json()["error"]["fields"]
    assert Contact.objects.count() == 0
    assert TransactionalMessage.objects.count() == 0
    assert EmailEvent.objects.count() == 0
    assert enqueued == []


def test_transactional_models_are_available_in_admin():
    assert isinstance(admin.site._registry[EmailTemplate], EmailTemplateAdmin)
    assert isinstance(admin.site._registry[TransactionalMessage], TransactionalMessageAdmin)
    assert isinstance(admin.site._registry[EmailEvent], EmailEventAdmin)


@pytest.mark.aws_local
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


@pytest.mark.aws_local
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
