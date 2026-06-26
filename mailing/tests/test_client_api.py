from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CategoryPreference,
    Client,
    ClientApiKey,
    CmpCallback,
    Contact,
    ContactSourceMetadata,
    ContactTag,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    EmailValidationStatus,
    Organization,
    RecipientList,
    RecipientListImportJob,
    RecipientListImportJobStatus,
    RecipientListMember,
    Subscription,
    SubscriptionStatus,
    Tag,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.auth import authenticate_bearer_token, check_api_key, create_client_api_key
from mailing.services.campaigns import snapshot_campaign_recipients
from mailing.services.recipient_lists import process_recipient_list_import_job

pytestmark = pytest.mark.django_db

API_KEY = "test-client-key"


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def api_client_record(organization):
    client = Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
    )
    create_client_api_key(client=client, name="Test key", raw_api_key=API_KEY)
    return client


@pytest.fixture
def other_org():
    return Organization.objects.create(name="AI Shipping Labs", slug="ai-shipping-labs")


@pytest.fixture
def other_audience(other_org):
    return Audience.objects.create(organization=other_org, name="AI Shipping Labs", slug="datatalks-club")


@pytest.fixture
def other_client(other_org):
    client = Client.objects.create(
        organization=other_org,
        name="AI Shipping Labs",
        slug="ai-shipping-labs",
    )
    create_client_api_key(client=client, name="Other key", raw_api_key="other-key")
    return client


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def post_json(django_client, url_name, payload, raw_key=API_KEY):
    return django_client.post(
        reverse(url_name),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def put_json(django_client, url_name, payload, *args, raw_key=API_KEY):
    return django_client.put(
        reverse(url_name, args=args),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def patch_json(django_client, url_name, payload, *args, raw_key=API_KEY):
    return django_client.patch(
        reverse(url_name, args=args),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def delete_json(django_client, url_name, payload, *args, raw_key=API_KEY):
    return django_client.delete(
        reverse(url_name, args=args),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def test_api_key_hash_helper_does_not_store_or_match_raw_key(api_client_record):
    api_key = ClientApiKey.objects.get(client=api_client_record, name="Test key")
    assert api_key.key_hash != API_KEY
    assert check_api_key(API_KEY, api_key.key_hash) is True
    assert check_api_key("wrong", api_key.key_hash) is False


def test_authentication_rejects_missing_unknown_and_inactive_clients(client, api_client_record):
    response = client.post(reverse("mailing:api_contacts"), data={}, content_type="application/json")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_authorization"

    response = client.post(
        reverse("mailing:api_contacts"),
        data={},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Token {API_KEY}",
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_authorization"

    response = post_json(client, "mailing:api_contacts", {}, raw_key="unknown-key")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"

    api_client_record.is_active = False
    api_client_record.save(update_fields=["is_active"])
    response = post_json(client, "mailing:api_contacts", {}, raw_key=API_KEY)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "inactive_client"
    assert Contact.objects.count() == 0


def test_authentication_helper_returns_active_client(api_client_record):
    result = authenticate_bearer_token(f"Bearer {API_KEY}")

    assert result.is_authenticated is True
    assert result.client.id == api_client_record.id
    api_key = ClientApiKey.objects.get(client=api_client_record, name="Test key")
    assert api_key.last_used_at is not None


def test_authentication_accepts_multiple_active_keys_and_rejects_revoked_key(api_client_record):
    second_key, second_raw = create_client_api_key(client=api_client_record, name="CI staging")

    assert authenticate_bearer_token(f"Bearer {API_KEY}").client == api_client_record
    assert authenticate_bearer_token(f"Bearer {second_raw}").client == api_client_record

    first_key = ClientApiKey.objects.get(client=api_client_record, name="Test key")
    first_key.revoked_at = timezone.now()
    first_key.save(update_fields=["revoked_at", "updated_at"])

    assert authenticate_bearer_token(f"Bearer {API_KEY}").error == "invalid_api_key"
    assert authenticate_bearer_token(f"Bearer {second_raw}").client == api_client_record
    second_key.refresh_from_db()
    assert second_key.last_used_at is not None


def test_contact_upsert_is_idempotent_and_scoped_to_authenticated_client(client, audience, api_client_record):
    payload = {
        "email": " Person@Example.COM ",
        "audience": audience.slug,
        "client": api_client_record.slug,
        "tags": ["ML Zoomcamp", "lead"],
        "status": "subscribed",
    }

    first = post_json(client, "mailing:api_contacts", payload)
    second = post_json(client, "mailing:api_contacts", payload)

    assert first.status_code == 200
    assert second.status_code == 200
    body = second.json()
    assert body["email"] == "person@example.com"
    assert body["verified"] is False
    assert body["client"]["status"] == SubscriptionStatus.SUBSCRIBED
    assert body["client"]["verified"] is False
    assert body["can_send_marketing"] is False
    assert body["can_send_transactional"] is True
    assert body["tags"] == ["lead", "ml-zoomcamp"]
    assert Contact.objects.count() == 1
    assert Subscription.objects.count() == 1
    assert Tag.objects.count() == 2
    assert ContactTag.objects.count() == 2


def test_recipient_list_member_upsert_creates_list_and_member_idempotently(client, audience, api_client_record):
    list_key = "ml-zoomcamp-2026:@e:@homework:homework-1"
    source_key = "homework-submission:42"
    payload = {
        "audience": audience.slug,
        "client": api_client_record.slug,
        "list": {
            "type": "homework_submitters",
            "name": "ML Zoomcamp 2026 Homework 1 submitters",
            "metadata": {"course": "ml-zoomcamp-2026", "homework": "homework-1"},
        },
        "member": {
            "email": " Learner@Example.COM ",
            "status": "active",
            "metadata": {"submission_id": 42},
        },
    }

    first = put_json(client, "mailing:api_recipient_list_member", payload, list_key, source_key)
    second = put_json(client, "mailing:api_recipient_list_member", payload, list_key, source_key)

    assert first.status_code == 200
    assert second.status_code == 200
    body = second.json()
    assert body["created"] is False
    assert body["recipient_list"]["key"] == list_key
    assert body["recipient_list"]["active_member_count"] == 1
    assert body["member"]["source_object_key"] == source_key
    assert body["member"]["email"] == "learner@example.com"
    assert Contact.objects.count() == 1
    assert set(RecipientList.objects.values_list("key", flat=True)) == {
        list_key,
        "ml-zoomcamp-2026:@e:@homework",
        "ml-zoomcamp-2026:@e",
        "ml-zoomcamp-2026",
        "<all>",
    }
    assert RecipientListMember.objects.count() == 5
    course_member = RecipientListMember.objects.get(recipient_list__key="ml-zoomcamp-2026")
    assert course_member.active is True
    assert course_member.source_object_key.startswith("cascade-contact:")
    assert course_member.metadata["membership_reasons"] == [
        {
            "list_key": list_key,
            "source_object_key": source_key,
        }
    ]

    get_response = client.get(
        reverse("mailing:api_recipient_list", args=[list_key]),
        {"audience": audience.slug, "client": api_client_record.slug},
        **auth_headers(),
    )
    assert get_response.status_code == 200
    assert get_response.json()["recipient_list"]["member_count"] == 1


def test_recipient_list_reconcile_removes_cascaded_parent_reason(client, audience, api_client_record):
    list_key = "ml-zoomcamp-2026:@e:@homework:homework-1"
    payload = {
        "audience": audience.slug,
        "client": api_client_record.slug,
        "list": {
            "type": "homework_submitters",
            "name": "ML Zoomcamp 2026 Homework 1 submitters",
        },
        "members": [
            {
                "source_object_key": "homework-submission:1",
                "email": "learner@example.com",
                "metadata": {"submission_id": 1},
            }
        ],
    }
    bulk_response = client.post(
        reverse("mailing:api_recipient_list_bulk_upsert", args=[list_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(),
    )
    assert bulk_response.status_code == 200

    reconcile_response = client.post(
        reverse("mailing:api_recipient_list_reconcile", args=[list_key]),
        data={**payload, "members": [], "remove_absent": True},
        content_type="application/json",
        **auth_headers(),
    )

    assert reconcile_response.status_code == 200
    assert reconcile_response.json()["removed_count"] == 1
    leaf_member = RecipientListMember.objects.get(recipient_list__key=list_key)
    course_member = RecipientListMember.objects.get(recipient_list__key="ml-zoomcamp-2026")
    assert leaf_member.active is False
    assert course_member.active is False
    assert course_member.metadata["membership_reasons"] == []


def test_recipient_list_keys_are_scoped_to_authenticated_client(
    client,
    audience,
    api_client_record,
    other_audience,
    other_client,
):
    list_key = "course-registrants:ml-zoomcamp-2026"
    payload = {
        "audience": audience.slug,
        "client": api_client_record.slug,
        "type": "registrants",
        "name": "ML Zoomcamp 2026 registrants",
    }
    other_payload = {
        "audience": other_audience.slug,
        "client": other_client.slug,
        "type": "registrants",
        "name": "Other registrants",
    }

    response = put_json(client, "mailing:api_recipient_list", payload, list_key)
    other_response = put_json(client, "mailing:api_recipient_list", other_payload, list_key, raw_key="other-key")

    assert response.status_code == 200
    assert other_response.status_code == 200
    assert RecipientList.objects.filter(key=list_key).count() == 2


def test_recipient_list_bulk_upsert_and_reconcile(client, audience, api_client_record):
    list_key = "course-registrants:ml-zoomcamp-2026"
    dry_run_key = "course-registrants:dry-run"
    base_payload = {
        "audience": audience.slug,
        "client": api_client_record.slug,
        "list": {
            "type": "registrants",
            "name": "ML Zoomcamp 2026 registrants",
        },
        "members": [
            {
                "source_object_key": "registration:1",
                "email": "one@example.com",
                "metadata": {"user_id": 1},
            },
            {
                "source_object_key": "registration:2",
                "email": "two@example.com",
                "metadata": {"user_id": 2},
            },
        ],
    }

    dry_run_response = client.post(
        reverse("mailing:api_recipient_list_reconcile", args=[dry_run_key]),
        data={**base_payload, "dry_run": True},
        content_type="application/json",
        **auth_headers(),
    )
    assert dry_run_response.status_code == 200
    assert dry_run_response.json()["dry_run"] is True
    assert RecipientList.objects.filter(key=dry_run_key).exists() is False

    bulk_response = client.post(
        reverse("mailing:api_recipient_list_bulk_upsert", args=[list_key]),
        data=base_payload,
        content_type="application/json",
        **auth_headers(),
    )
    assert bulk_response.status_code == 200
    assert bulk_response.json()["created_count"] == 2

    reconcile_payload = {
        **base_payload,
        "members": [base_payload["members"][0]],
        "remove_absent": True,
    }
    reconcile_response = client.post(
        reverse("mailing:api_recipient_list_reconcile", args=[list_key]),
        data=reconcile_payload,
        content_type="application/json",
        **auth_headers(),
    )

    assert reconcile_response.status_code == 200
    body = reconcile_response.json()
    assert body["removed_count"] == 1
    assert body["recipient_list"]["member_count"] == 2
    assert body["recipient_list"]["active_member_count"] == 1
    assert RecipientListMember.objects.get(source_object_key="registration:1").active is True
    removed = RecipientListMember.objects.get(source_object_key="registration:2")
    assert removed.active is False
    assert removed.removed_at is not None


def test_recipient_list_import_job_fetches_jsonl_and_updates_members(
    client,
    audience,
    api_client_record,
    monkeypatch,
):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _size):
            return b"\n".join(
                [
                    b'{"source_object_key":"registration:1","email":"one@example.com","metadata":{"user_id":1}}',
                    b'{"source_object_key":"registration:2","email":"two@example.com","metadata":{"user_id":2}}',
                ]
            )

    monkeypatch.setattr("mailing.services.recipient_lists.urlopen", lambda *args, **kwargs: FakeResponse())
    list_key = "ml-zoomcamp-2026:@e"
    payload = {
        "audience": audience.slug,
        "client": api_client_record.slug,
        "source_url": "https://storage.example.com/import.jsonl?signature=abc",
        "idempotency_key": "cmp-import-1",
        "list": {
            "name": "ML Zoomcamp enrolled",
            "metadata": {"course_slug": "ml-zoomcamp-2026"},
        },
    }

    response = client.post(
        reverse("mailing:api_recipient_list_imports", args=[list_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(),
    )
    replay = client.post(
        reverse("mailing:api_recipient_list_imports", args=[list_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(),
    )

    assert response.status_code == 202
    assert replay.status_code == 200
    body = response.json()
    job_id = body["import_job"]["id"]
    assert body["created"] is True
    assert replay.json()["created"] is False
    assert replay.json()["import_job"]["id"] == job_id
    assert body["import_job"]["status"] == RecipientListImportJobStatus.PENDING

    job = process_recipient_list_import_job(job_id)

    assert job.status == RecipientListImportJobStatus.SUCCEEDED
    assert job.row_count == 2
    assert job.created_count == 2
    assert job.content_sha256
    assert RecipientList.objects.get(key=list_key).active_member_count == 2
    assert set(RecipientListMember.objects.filter(recipient_list__key=list_key).values_list("email", flat=True)) == {
        "one@example.com",
        "two@example.com",
    }

    detail = client.get(
        reverse("mailing:api_recipient_list_import", args=[list_key, job_id]),
        {"audience": audience.slug, "client": api_client_record.slug},
        **auth_headers(),
    )

    assert detail.status_code == 200
    detail_job = detail.json()["import_job"]
    assert detail_job["status"] == RecipientListImportJobStatus.SUCCEEDED
    assert detail_job["recipient_list"]["active_member_count"] == 2


def test_recipient_list_import_job_records_bad_rows_without_writes(
    client,
    audience,
    api_client_record,
    monkeypatch,
):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _size):
            return b'{"source_object_key":"registration:1","email":"not-an-email"}'

    monkeypatch.setattr("mailing.services.recipient_lists.urlopen", lambda *args, **kwargs: FakeResponse())
    list_key = "ml-zoomcamp-2026:@e"
    payload = {
        "audience": audience.slug,
        "client": api_client_record.slug,
        "source_url": "https://storage.example.com/import.jsonl",
    }

    response = client.post(
        reverse("mailing:api_recipient_list_imports", args=[list_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(),
    )
    job = process_recipient_list_import_job(response.json()["import_job"]["id"])

    assert job.status == RecipientListImportJobStatus.FAILED
    assert job.failed_count == 1
    assert job.failed_rows[0]["line"] == 1
    assert RecipientListImportJob.objects.count() == 1
    assert RecipientList.objects.filter(key=list_key).exists() is False


def test_recipient_list_import_worker_command_processes_one_batch():
    out = StringIO()

    call_command("process_recipient_list_imports", "--once", stdout=out)

    assert "processed=0 succeeded=0 failed=0" in out.getvalue()


def test_contact_upsert_accepts_validation_and_suppression_inputs_idempotently(client, audience, api_client_record):
    payload = {
        "email": "person@example.com",
        "audience": audience.slug,
        "client": api_client_record.slug,
        "status": SubscriptionStatus.SUBSCRIBED,
        "verified": True,
        "email_validation": {
            "status": EmailValidationStatus.EXTERNALLY_VALIDATED,
            "reason": "provider hygiene import",
        },
        "suppression": {
            "global_unsubscribed": False,
            "hard_bounced": False,
            "complained": False,
        },
    }

    first = post_json(client, "mailing:api_contacts", payload)
    second = post_json(client, "mailing:api_contacts", payload)

    contact = Contact.objects.get()
    assert first.status_code == 200
    assert second.status_code == 200
    assert contact.email_validation_status == EmailValidationStatus.EXTERNALLY_VALIDATED
    assert contact.email_validation_reason == "provider hygiene import"
    assert contact.email_validated_at is not None
    assert contact.global_unsubscribed_at is None
    assert second.json()["contact_id"] == contact.id
    assert second.json()["email_validation"]["status"] == EmailValidationStatus.EXTERNALLY_VALIDATED
    assert Contact.objects.count() == 1
    assert Subscription.objects.count() == 1


def test_contact_preferences_api_reads_defaults_and_updates_categories(client, audience, api_client_record):
    get_response = client.get(
        reverse("mailing:api_contact_preferences"),
        {
            "email": "Learner@Example.com",
            "audience": audience.slug,
            "client": api_client_record.slug,
            "category_tags": "submission-results,deadline-reminders,course-updates",
        },
        **auth_headers(),
    )

    assert get_response.status_code == 200
    assert get_response.json()["categories"] == [
        {"tag": "submission-results", "label": "Submission Results", "enabled": True},
        {"tag": "deadline-reminders", "label": "Deadline Reminders", "enabled": True},
        {"tag": "course-updates", "label": "Course Updates", "enabled": True},
    ]

    put_response = put_json(
        client,
        "mailing:api_contact_preferences",
        {
            "email": "Learner@Example.com",
            "audience": audience.slug,
            "client": api_client_record.slug,
            "categories": [
                {
                    "tag": "submission-results",
                    "label": "Homework and project submissions",
                    "enabled": False,
                },
                {
                    "tag": "deadline-reminders",
                    "label": "Deadline reminders",
                    "enabled": True,
                },
            ],
        },
    )

    assert put_response.status_code == 200
    assert put_response.json()["email"] == "learner@example.com"
    assert put_response.json()["categories"] == [
        {
            "tag": "submission-results",
            "label": "Homework and project submissions",
            "enabled": False,
        },
        {
            "tag": "deadline-reminders",
            "label": "Deadline reminders",
            "enabled": True,
        },
    ]
    assert Contact.objects.get().normalized_email == "learner@example.com"
    assert CategoryPreference.objects.filter(enabled=False, tag="submission-results").exists()


def test_contact_preferences_reject_enabling_suppressed_contact(client, audience, api_client_record):
    contact = Contact.objects.create(email="suppressed@example.com", hard_bounced_at=timezone.now())

    response = put_json(
        client,
        "mailing:api_contact_preferences",
        {
            "email": contact.email,
            "audience": audience.slug,
            "client": api_client_record.slug,
            "categories": [
                {
                    "tag": "course-updates",
                    "label": "Course updates",
                    "enabled": True,
                }
            ],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["fields"] == {
        "categories": "suppressed_contact_cannot_be_enabled",
    }


def test_contact_erase_deletes_live_state_and_anonymizes_history(client, audience, api_client_record):
    contact = Contact.objects.create(
        email="person@example.com",
        verified_at=timezone.now(),
        email_validation_status=EmailValidationStatus.EXTERNALLY_VALIDATED,
        email_validated_at=timezone.now(),
        global_unsubscribed_at=timezone.now(),
        hard_bounced_at=timezone.now(),
        complained_at=timezone.now(),
    )
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    CategoryPreference.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        tag="course-updates",
        enabled=False,
    )
    tag = Tag.objects.create(audience=audience, name="Learner", slug="learner")
    ContactTag.objects.create(contact=contact, tag=tag)
    ContactSourceMetadata.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        source="cmp",
        external_id="user:42",
        metadata={"email": "person@example.com"},
    )
    recipient_list = RecipientList.objects.create(
        audience=audience,
        client=api_client_record,
        key="ml-zoomcamp-2026",
        name="ML Zoomcamp 2026",
    )
    RecipientListMember.objects.create(
        recipient_list=recipient_list,
        contact=contact,
        email="person@example.com",
        source_object_key="user:42",
    )
    template = EmailTemplate.objects.create(
        client=api_client_record,
        key="submission-result",
        name="Submission result",
        subject="Result",
    )
    message = TransactionalMessage.objects.create(
        client=api_client_record,
        contact=contact,
        email="person@example.com",
        template=template,
        template_key=template.key,
        subject="Result",
        context={"email": "person@example.com"},
        metadata={"source": "cmp"},
    )
    campaign = Campaign.objects.create(
        audience=audience,
        client=api_client_record,
        subject="Course starts",
    )
    recipient = CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email="person@example.com",
    )
    event = EmailEvent.objects.create(
        campaign=campaign,
        campaign_recipient=recipient,
        transactional_message=message,
        contact=contact,
        audience=audience,
        client=api_client_record,
        event_type=EmailEventType.SENT,
        metadata={"email": "person@example.com"},
    )
    callback = CmpCallback.objects.create(
        email_event=event,
        contact=contact,
        audience=audience,
        client=api_client_record,
        event_id="evt_1",
        event_type="sent",
        callback_url="https://courses.example.com/webhook",
        payload={"email": "person@example.com"},
        next_attempt_at=timezone.now(),
    )

    response = post_json(
        client,
        "mailing:api_contact_erase",
        {
            "email": "person@example.com",
            "audience": audience.slug,
            "client": api_client_record.slug,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["erased"] is True
    assert body["email"] == "person@example.com"
    assert body["counts"]["subscriptions_deleted"] == 1
    assert body["counts"]["recipient_list_members_deleted"] == 1
    contact.refresh_from_db()
    assert contact.email == f"erased-contact-{contact.id}@erased.invalid"
    assert contact.normalized_email == contact.email
    assert contact.verified_at is None
    assert contact.email_validation_status == EmailValidationStatus.UNKNOWN
    assert contact.global_unsubscribed_at is None
    assert Subscription.objects.filter(contact=contact).exists() is False
    assert CategoryPreference.objects.filter(contact=contact).exists() is False
    assert ContactTag.objects.filter(contact=contact).exists() is False
    assert ContactSourceMetadata.objects.filter(contact=contact).exists() is False
    assert RecipientListMember.objects.filter(contact=contact).exists() is False
    message.refresh_from_db()
    recipient.refresh_from_db()
    event.refresh_from_db()
    callback.refresh_from_db()
    assert message.email == contact.email
    assert message.context == {}
    assert message.metadata == {"erased": True}
    assert recipient.email == contact.email
    assert event.metadata == {"erased": True}
    assert callback.payload == {"erased": True}
    assert Contact.objects.filter(normalized_email="person@example.com").exists() is False

    second_response = post_json(
        client,
        "mailing:api_contact_erase",
        {
            "email": "person@example.com",
            "audience": audience.slug,
            "client": api_client_record.slug,
        },
    )

    assert second_response.status_code == 200
    assert second_response.json()["erased"] is False


def test_contact_upsert_verified_marks_subscription_not_global_contact(client, audience, api_client_record):
    response = post_json(
        client,
        "mailing:api_contacts",
        {
            "email": "person@example.com",
            "audience": audience.slug,
            "client": api_client_record.slug,
            "status": "subscribed",
            "verified": True,
        },
    )

    contact = Contact.objects.get()
    subscription = Subscription.objects.get()
    assert response.status_code == 200
    assert contact.verified_at is None
    assert subscription.verified_at is not None
    assert response.json()["can_send_marketing"] is True


def test_contact_upsert_verified_contact_is_campaign_eligible(client, audience, api_client_record):
    response = post_json(
        client,
        "mailing:api_contacts",
        {
            "email": "person@example.com",
            "audience": audience.slug,
            "client": api_client_record.slug,
            "status": "subscribed",
            "verified": True,
        },
    )
    campaign = Campaign.objects.create(
        audience=audience,
        client=api_client_record,
        subject="Course starts",
    )

    result = snapshot_campaign_recipients(campaign)

    recipient = CampaignRecipient.objects.get(campaign=campaign)
    assert response.status_code == 200
    assert result.recipient_count == 1
    assert result.skipped_count == 0
    assert recipient.email == "person@example.com"
    assert recipient.status == CampaignRecipientStatus.PENDING
    assert recipient.skip_reason == ""


def test_contact_tag_mutations_are_idempotent_and_campaign_filter_compatible(client, audience, api_client_record):
    contact = Contact.objects.create(email="person@example.com")
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        status=SubscriptionStatus.SUBSCRIBED,
    )

    payload = {"audience": audience.slug, "client": api_client_record.slug, "tags": ["ML Zoomcamp", "lead"]}
    replace_first = put_json(client, "mailing:api_contact_tags", payload, contact.id)
    replace_second = put_json(client, "mailing:api_contact_tags", payload, contact.id)
    add_first = client.post(
        reverse("mailing:api_contact_tag", args=[contact.id, "data-engineering"]),
        data={"audience": audience.slug, "client": api_client_record.slug},
        content_type="application/json",
        **auth_headers(),
    )
    add_second = client.post(
        reverse("mailing:api_contact_tag", args=[contact.id, "data-engineering"]),
        data={"audience": audience.slug, "client": api_client_record.slug},
        content_type="application/json",
        **auth_headers(),
    )
    remove_first = delete_json(
        client,
        "mailing:api_contact_tag",
        {"audience": audience.slug, "client": api_client_record.slug},
        contact.id,
        "lead",
    )
    remove_second = delete_json(
        client,
        "mailing:api_contact_tag",
        {"audience": audience.slug, "client": api_client_record.slug},
        contact.id,
        "lead",
    )

    assert replace_first.status_code == 200
    assert replace_second.status_code == 200
    assert add_first.status_code == 200
    assert add_second.status_code == 200
    assert remove_first.status_code == 200
    assert remove_second.status_code == 200
    assert remove_second.json()["tags"] == ["data-engineering", "ml-zoomcamp"]
    assert list(
        ContactTag.objects.filter(contact=contact, tag__audience=audience)
        .values_list("tag__slug", flat=True)
        .order_by("tag__slug")
    ) == [
        "data-engineering",
        "ml-zoomcamp",
    ]
    campaign = Campaign.objects.create(
        audience=audience,
        client=api_client_record,
        subject="Course starts",
        include_tags=["ML Zoomcamp"],
        exclude_tags=["Lead"],
    )
    assert campaign.include_tags == ["ml-zoomcamp"]
    assert campaign.exclude_tags == ["lead"]


def test_contact_mutation_endpoints_update_verification_validation_and_suppression(
    client,
    audience,
    api_client_record,
):
    contact = Contact.objects.create(email="person@example.com")
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    scope = {"audience": audience.slug, "client": api_client_record.slug}

    verification = patch_json(
        client,
        "mailing:api_contact_verification",
        scope | {"verified": True},
        contact.id,
    )
    validation = patch_json(
        client,
        "mailing:api_contact_validation",
        scope | {"status": EmailValidationStatus.NO_MX, "reason": "external hygiene"},
        contact.id,
    )
    suppression = patch_json(
        client,
        "mailing:api_contact_suppression",
        scope | {"global_unsubscribed": True, "hard_bounced": True, "complained": False, "reason": "api"},
        contact.id,
    )
    suppression_repeat = patch_json(
        client,
        "mailing:api_contact_suppression",
        scope | {"global_unsubscribed": True, "hard_bounced": True, "complained": False, "reason": "api"},
        contact.id,
    )

    contact.refresh_from_db()
    subscription = Subscription.objects.get()
    assert verification.status_code == 200
    assert subscription.verified_at is not None
    assert contact.verified_at is not None
    assert validation.status_code == 200
    assert contact.email_validation_status == EmailValidationStatus.NO_MX
    assert contact.email_validation_reason == "external hygiene"
    assert contact.email_validated_at is not None
    assert suppression.status_code == 200
    assert suppression_repeat.status_code == 200
    assert contact.global_unsubscribed_at is not None
    assert contact.hard_bounced_at is not None
    assert contact.complained_at is None
    assert suppression.json()["can_send_marketing"] is False
    assert suppression.json()["can_send_transactional"] is False
    assert EmailEvent.objects.filter(event_type=EmailEventType.UNSUBSCRIBE).count() == 1
    assert EmailEvent.objects.filter(event_type=EmailEventType.BOUNCE).count() == 1


def test_contact_mutation_endpoints_reject_unscoped_contact_ids(client, audience, api_client_record, other_client):
    contact = Contact.objects.create(email="person@example.com")
    Subscription.objects.create(contact=contact, audience=audience, client=other_client)

    response = put_json(
        client,
        "mailing:api_contact_tags",
        {"audience": audience.slug, "client": api_client_record.slug, "tags": ["news"]},
        contact.id,
    )

    assert response.status_code == 404
    assert response.json()["error"]["fields"]["contact_id"] == "not_found"
    assert ContactTag.objects.count() == 0


def test_contact_status_returns_subscription_suppression_and_eligibility(client, audience, api_client_record):
    contact = Contact.objects.create(
        email="person@example.com",
        email_validation_status=EmailValidationStatus.VALID,
        email_validation_reason="imported hygiene check",
        email_validated_at=timezone.now(),
    )
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        status=SubscriptionStatus.SUBSCRIBED,
    )

    response = client.get(
        reverse("mailing:api_contact_status"),
        {"email": "PERSON@example.com", "audience": audience.slug, "client": api_client_record.slug},
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    assert body["verified"] is False
    assert body["email_validation"]["status"] == EmailValidationStatus.VALID
    assert body["email_validation"]["reason"] == "imported hygiene check"
    assert body["email_validation"]["validated_at"] is not None
    assert body["global_unsubscribed"] is False
    assert body["hard_bounced"] is False
    assert body["complained"] is False
    assert body["client"]["subscribed"] is True
    assert body["can_send_marketing"] is False
    assert body["can_send_transactional"] is True

    contact.verified_at = timezone.now()
    contact.global_unsubscribed_at = timezone.now()
    contact.save(update_fields=["verified_at", "global_unsubscribed_at", "updated_at"])
    response = client.get(
        reverse("mailing:api_contact_status"),
        {"email": "person@example.com", "audience": audience.slug, "client": api_client_record.slug},
        **auth_headers(),
    )
    assert response.json()["global_unsubscribed"] is True
    assert response.json()["can_send_marketing"] is False
    assert response.json()["can_send_transactional"] is True

    contact.hard_bounced_at = timezone.now()
    contact.save(update_fields=["hard_bounced_at", "updated_at"])
    response = client.get(
        reverse("mailing:api_contact_status"),
        {"email": "person@example.com", "audience": audience.slug, "client": api_client_record.slug},
        **auth_headers(),
    )
    assert response.json()["hard_bounced"] is True
    assert response.json()["can_send_transactional"] is False

    contact.hard_bounced_at = None
    contact.global_unsubscribed_at = None
    contact.email_validation_status = EmailValidationStatus.MANUALLY_INVALID
    contact.email_validation_reason = "operator review"
    contact.save(
        update_fields=[
            "hard_bounced_at",
            "global_unsubscribed_at",
            "email_validation_status",
            "email_validation_reason",
            "updated_at",
        ]
    )
    response = client.get(
        reverse("mailing:api_contact_status"),
        {"email": "person@example.com", "audience": audience.slug, "client": api_client_record.slug},
        **auth_headers(),
    )
    assert response.json()["email_validation"]["status"] == EmailValidationStatus.MANUALLY_INVALID
    assert response.json()["email_validation"]["reason"] == "operator review"
    assert response.json()["can_send_marketing"] is False


def test_status_lookup_does_not_expose_contact_without_authenticated_client_subscription(
    client,
    audience,
    api_client_record,
):
    Contact.objects.create(email="person@example.com", verified_at=timezone.now())

    response = client.get(
        reverse("mailing:api_contact_status"),
        {"email": "person@example.com", "audience": audience.slug, "client": api_client_record.slug},
        **auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()["exists"] is False
    assert response.json()["email_validation"]["status"] == EmailValidationStatus.UNKNOWN
    assert response.json()["can_send_transactional"] is False


def test_subscribe_creates_or_restores_authenticated_client_subscription(client, audience, api_client_record):
    contact = Contact.objects.create(email="person@example.com")
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        status=SubscriptionStatus.UNSUBSCRIBED,
        unsubscribe_reason="old",
    )

    response = post_json(
        client,
        "mailing:api_subscribe",
        {"email": "person@example.com", "audience": audience.slug, "client": api_client_record.slug, "tags": ["news"]},
    )

    subscription = Subscription.objects.get()
    assert response.status_code == 200
    assert subscription.status == SubscriptionStatus.SUBSCRIBED
    assert subscription.unsubscribe_reason == ""
    assert response.json()["client"]["verified"] is True
    assert response.json()["tags"] == ["news"]


def test_unsubscribe_supports_client_audience_and_global_scopes_idempotently(client, audience, api_client_record):
    contact = Contact.objects.create(email="person@example.com")
    Subscription.objects.create(contact=contact, audience=audience, client=api_client_record)

    first = post_json(
        client,
        "mailing:api_unsubscribe",
        {
            "email": "person@example.com",
            "audience": audience.slug,
            "client": api_client_record.slug,
            "scope": "client",
            "reason": "api_request",
        },
    )
    second = post_json(
        client,
        "mailing:api_unsubscribe",
        {
            "email": "person@example.com",
            "audience": audience.slug,
            "client": api_client_record.slug,
            "scope": "client",
            "reason": "api_request",
        },
    )
    subscription = Subscription.objects.get(client=api_client_record)
    assert first.status_code == 200
    assert second.status_code == 200
    assert subscription.status == SubscriptionStatus.UNSUBSCRIBED
    assert subscription.unsubscribe_reason == "api_request"
    assert Subscription.objects.filter(client=api_client_record).count() == 1

    response = post_json(
        client,
        "mailing:api_unsubscribe",
        {
            "email": "person@example.com",
            "audience": audience.slug,
            "client": api_client_record.slug,
            "scope": "audience",
            "reason": "audience_request",
        },
    )
    assert response.status_code == 200
    audience_subscription = Subscription.objects.get(client__isnull=True)
    assert audience_subscription.status == SubscriptionStatus.UNSUBSCRIBED
    assert audience_subscription.unsubscribe_reason == "audience_request"

    response = post_json(
        client,
        "mailing:api_unsubscribe",
        {
            "email": "person@example.com",
            "audience": audience.slug,
            "client": api_client_record.slug,
            "scope": "global",
            "reason": "global_request",
        },
    )
    contact.refresh_from_db()
    assert response.status_code == 200
    assert contact.global_unsubscribed_at is not None


def test_contact_history_is_scoped_and_does_not_expose_tokens_or_secrets(client, audience, api_client_record):
    contact = Contact.objects.create(email="person@example.com")
    Subscription.objects.create(contact=contact, audience=audience, client=api_client_record)
    campaign = Campaign.objects.create(audience=audience, client=api_client_record, subject="Newsletter")
    recipient = CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        tracking_token_hash="hashed-tracking-token",
        unsubscribe_token_hash="hashed-unsubscribe-token",
        sent_at=timezone.now(),
        open_count=1,
        click_count=1,
    )
    template = EmailTemplate.objects.create(
        client=api_client_record,
        key="welcome",
        name="Welcome",
        subject="Welcome",
        is_transactional=True,
    )
    message = TransactionalMessage.objects.create(
        client=api_client_record,
        contact=contact,
        email=contact.email,
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.SENT,
        idempotency_key="client-event-1",
        subject="Welcome",
        context={"verification_url": "https://example.test/verify/raw-token"},
        metadata={"api_key": API_KEY},
    )
    EmailEvent.objects.create(
        campaign=campaign,
        campaign_recipient=recipient,
        contact=contact,
        client=api_client_record,
        audience=audience,
        event_type=EmailEventType.CLICK,
        url="https://example.test/account?token=raw-token",
        metadata={"scope": "client", "api_key": API_KEY, "source": "ses"},
    )
    EmailEvent.objects.create(
        transactional_message=message,
        contact=contact,
        client=api_client_record,
        event_type=EmailEventType.SENT,
        metadata={"ses_message_id": "ses-123", "secret": "hidden"},
    )

    response = client.get(
        reverse("mailing:api_contact_history", args=[contact.id]),
        {"audience": audience.slug, "client": api_client_record.slug, "limit": "1"},
        **auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["contact_id"] == contact.id
    assert body["campaign_recipients"][0]["id"] == recipient.id
    assert body["transactional_messages"][0]["id"] == message.id
    assert body["events"][0]["metadata"] == {"ses_message_id": "ses-123"}
    serialized = str(body)
    assert "hashed-tracking-token" not in serialized
    assert "hashed-unsubscribe-token" not in serialized
    assert "raw-token" not in serialized
    assert API_KEY not in serialized
    assert "secret" not in serialized
    assert body["next_cursor"] is not None


def test_api_errors_are_json_only_and_never_login_redirect(client, api_client_record):
    response = client.get(reverse("mailing:api_contacts"), follow=False)

    assert response.status_code == 401
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.json()["error"]["code"] == "missing_authorization"

    response = client.get(reverse("mailing:api_contact_status"), follow=False)
    assert response.status_code == 401
    assert response.headers["Content-Type"].startswith("application/json")
    assert "Location" not in response.headers


def test_old_api_v1_routes_are_not_registered(client, api_client_record):
    headers = auth_headers()

    contacts = client.post("/api/v1/contacts", data={}, content_type="application/json", **headers)
    status = client.get(
        "/api/v1/contacts/status",
        {"email": "person@example.com", "audience": "datatalks-club", "client": api_client_record.slug},
        **headers,
    )
    transactional = client.post("/api/v1/transactional/send", data={}, content_type="application/json", **headers)

    assert contacts.status_code == 404
    assert status.status_code == 404
    assert transactional.status_code == 404


def test_cross_organization_slugs_are_rejected_without_mutation(
    client,
    other_audience,
    other_client,
    api_client_record,
):
    response = post_json(
        client,
        "mailing:api_contacts",
        {
            "email": "person@example.com",
            "audience": other_audience.slug,
            "client": other_client.slug,
            "status": "subscribed",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["fields"]["client"] == "forbidden"
    assert Contact.objects.count() == 0
    assert Subscription.objects.count() == 0
    assert ContactTag.objects.count() == 0


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"email": "not-email", "audience": "datatalks-club", "client": "dtc-courses"}, "email"),
        ({"email": "person@example.com", "client": "dtc-courses"}, "audience"),
        ({"email": "person@example.com", "audience": "datatalks-club"}, "client"),
        (
            {"email": "person@example.com", "audience": "datatalks-club", "client": "dtc-courses", "status": "bad"},
            "status",
        ),
        (
            {"email": "person@example.com", "audience": "datatalks-club", "client": "dtc-courses", "tags": "news"},
            "tags",
        ),
    ],
)
def test_validation_errors_are_structured_and_do_not_partially_mutate(
    client,
    audience,
    api_client_record,
    payload,
    field,
):
    response = post_json(client, "mailing:api_contacts", payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert field in response.json()["error"]["fields"]
    assert Contact.objects.count() == 0
    assert Subscription.objects.count() == 0
    assert ContactTag.objects.count() == 0
