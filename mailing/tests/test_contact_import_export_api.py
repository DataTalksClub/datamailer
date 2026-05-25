import csv
import io

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Client,
    Contact,
    ContactTag,
    EmailValidationStatus,
    Organization,
    Subscription,
    SubscriptionStatus,
    Tag,
)
from mailing.services.auth import create_client_api_key

pytestmark = pytest.mark.django_db

API_KEY = "bulk-api-key"


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="Newsletter", slug="newsletter")


@pytest.fixture
def second_audience(organization):
    return Audience.objects.create(organization=organization, name="Courses", slug="courses")


@pytest.fixture
def api_client_record(organization):
    client = Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
    )
    create_client_api_key(client=client, name="Bulk test", raw_api_key=API_KEY)
    return client


@pytest.fixture
def other_org():
    return Organization.objects.create(name="Other", slug="other")


@pytest.fixture
def other_audience(other_org):
    return Audience.objects.create(organization=other_org, name="Other", slug="newsletter")


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def post_json(django_client, url_name, payload, raw_key=API_KEY):
    return django_client.post(
        reverse(url_name),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def test_json_bulk_import_is_partial_idempotent_and_uses_single_contact_schema(
    client,
    audience,
    second_audience,
    api_client_record,
):
    existing = Contact.objects.create(email="update@example.com")
    Subscription.objects.create(contact=existing, audience=audience, client=api_client_record)
    payload = {
        "audience": audience.slug,
        "client": api_client_record.slug,
        "idempotency_key": "request-1",
        "contacts": [
            {
                "email": " Person@Example.COM ",
                "tags": ["ML Zoomcamp", "Lead"],
                "status": SubscriptionStatus.SUBSCRIBED,
                "verified": True,
                "email_validation": {
                    "status": EmailValidationStatus.VALID,
                    "reason": "external hygiene",
                },
                "suppression": {"global_unsubscribed": False, "hard_bounced": False, "complained": False},
            },
            {
                "email": " Person@Example.COM ",
                "audience": second_audience.slug,
                "tags": ["Courses"],
                "status": SubscriptionStatus.PENDING,
            },
            {"email": "bad-email", "status": SubscriptionStatus.SUBSCRIBED},
            {"email": " Person@Example.COM ", "status": SubscriptionStatus.UNSUBSCRIBED},
            {"email": "update@example.com", "tags": ["Updated"], "status": SubscriptionStatus.SUBSCRIBED},
        ],
    }

    first = post_json(client, "mailing:api_contact_imports", payload)
    second = post_json(client, "mailing:api_contact_imports", payload)

    assert first.status_code == 200
    assert first.json()["counts"] == {
        "total": 5,
        "created": 2,
        "updated": 1,
        "unchanged": 0,
        "skipped": 1,
        "invalid": 1,
    }
    assert first.json()["errors"][0]["errors"]["email"] == "invalid"
    duplicate_results = [result for result in first.json()["results"] if result.get("reason") == "duplicate_input"]
    assert duplicate_results == [
        {
            "index": 3,
            "item": 4,
            "email": "person@example.com",
            "action": "skipped",
            "reason": "duplicate_input",
            "kept_item": 1,
        }
    ]
    assert second.status_code == 200
    assert second.json()["counts"]["unchanged"] == 3
    assert second.json()["counts"]["created"] == 0

    contact = Contact.objects.get(normalized_email="person@example.com")
    subscription = Subscription.objects.get(contact=contact, audience=audience, client=api_client_record)
    second_subscription = Subscription.objects.get(contact=contact, audience=second_audience, client=api_client_record)
    assert subscription.status == SubscriptionStatus.SUBSCRIBED
    assert second_subscription.status == SubscriptionStatus.PENDING
    assert subscription.verified_at is not None
    assert contact.email_validation_status == EmailValidationStatus.VALID
    assert contact.email_validation_reason == "external hygiene"
    assert set(ContactTag.objects.filter(contact=contact, tag__audience=audience).values_list("tag__slug", flat=True)) == {
        "lead",
        "ml-zoomcamp",
    }
    assert set(ContactTag.objects.filter(contact=contact, tag__audience=second_audience).values_list("tag__slug", flat=True)) == {
        "courses"
    }
    assert set(existing.tags.values_list("slug", flat=True)) == {"updated"}


def test_json_bulk_import_enforces_authenticated_client_scope(client, other_audience, api_client_record):
    response = post_json(
        client,
        "mailing:api_contact_imports",
        {
            "audience": other_audience.slug,
            "client": api_client_record.slug,
            "contacts": [{"email": "person@example.com", "status": SubscriptionStatus.SUBSCRIBED}],
        },
    )

    assert response.status_code == 200
    assert response.json()["counts"]["invalid"] == 1
    assert response.json()["errors"][0]["errors"]["audience"] == "not_found"
    assert Contact.objects.count() == 0


def test_csv_api_import_reports_row_errors_and_reruns_idempotently(client, audience, second_audience, api_client_record):
    csv_body = "\n".join(
        [
            "email,audience,tags,subscription_status,verified,email_validation_status,email_validation_reason,global_unsubscribed",
            "valid@example.com,newsletter,Newsletter;ML,subscribed,true,externally_validated,provider,true",
            "valid@example.com,courses,Courses,pending,false,externally_validated,provider,true",
            "invalid-email,newsletter,Newsletter,subscribed,false,valid,,false",
            "unknown@example.com,newsletter,,pending,false,,,false",
        ]
    )

    first = post_json(
        client,
        "mailing:api_contact_imports_csv",
        {"audience": audience.slug, "client": api_client_record.slug, "csv": csv_body},
    )
    second = post_json(
        client,
        "mailing:api_contact_imports_csv",
        {"audience": audience.slug, "client": api_client_record.slug, "csv": csv_body},
    )

    assert first.status_code == 200
    assert first.json()["counts"]["created"] == 3
    assert first.json()["counts"]["invalid"] == 1
    assert first.json()["errors"][0]["row"] == 4
    assert second.status_code == 200
    assert second.json()["counts"]["unchanged"] == 3

    valid = Contact.objects.get(normalized_email="valid@example.com")
    assert Subscription.objects.filter(contact=valid, client=api_client_record).count() == 2
    assert valid.global_unsubscribed_at is not None
    assert valid.email_validation_status == EmailValidationStatus.EXTERNALLY_VALIDATED


def test_csv_api_import_rejects_malformed_missing_header_as_json_error(client, audience, api_client_record):
    response = post_json(
        client,
        "mailing:api_contact_imports_csv",
        {"audience": audience.slug, "client": api_client_record.slug, "csv": ""},
    )

    assert response.status_code == 400
    assert response.json()["error"]["fields"]["csv"] == "required"


def test_json_export_filters_and_cursor_pagination(client, audience, api_client_record):
    create_contact(
        "first@example.com",
        audience,
        api_client_record,
        tags=["newsletter", "ml"],
        status=SubscriptionStatus.SUBSCRIBED,
        verified=True,
        validation_status=EmailValidationStatus.VALID,
    )
    create_contact(
        "second@example.com",
        audience,
        api_client_record,
        tags=["newsletter"],
        status=SubscriptionStatus.PENDING,
        verified=False,
        validation_status=EmailValidationStatus.UNKNOWN,
    )
    create_contact(
        "third@example.com",
        audience,
        api_client_record,
        tags=["newsletter", "ml"],
        status=SubscriptionStatus.SUBSCRIBED,
        verified=True,
        validation_status=EmailValidationStatus.NO_MX,
        hard_bounced=True,
    )

    response = client.get(
        reverse("mailing:api_contacts"),
        {
            "audience": audience.slug,
            "client": api_client_record.slug,
            "tags": "newsletter,ml",
            "subscription_status": SubscriptionStatus.SUBSCRIBED,
            "verified": "true",
            "limit": "1",
        },
        **auth_headers(),
    )
    body = response.json()

    assert response.status_code == 200
    assert body["count"] == 1
    assert body["contacts"][0]["email"] == "first@example.com"
    assert body["next_cursor"] is not None

    next_response = client.get(
        reverse("mailing:api_contacts"),
        {
            "audience": audience.slug,
            "client": api_client_record.slug,
            "tags": "newsletter,ml",
            "subscription_status": SubscriptionStatus.SUBSCRIBED,
            "verified": "true",
            "cursor": body["next_cursor"],
        },
        **auth_headers(),
    )
    assert next_response.json()["contacts"][0]["email"] == "third@example.com"

    invalid_response = client.get(
        reverse("mailing:api_contacts"),
        {"audience": audience.slug, "client": api_client_record.slug, "email_validation_status": "bad"},
        **auth_headers(),
    )
    assert invalid_response.status_code == 400
    assert invalid_response.json()["error"]["fields"]["email_validation_status"] == "invalid"


def test_csv_export_api_and_command_are_safe_and_recreatable(client, tmp_path, audience, api_client_record):
    contact = create_contact(
        "safe@example.com",
        audience,
        api_client_record,
        tags=["newsletter"],
        status=SubscriptionStatus.UNSUBSCRIBED,
        verified=True,
        validation_status=EmailValidationStatus.MANUALLY_INVALID,
        global_unsubscribed=True,
    )
    contact.email_validation_reason = "manual review"
    contact.save(update_fields=["email_validation_reason", "updated_at"])

    response = client.get(
        reverse("mailing:api_contacts_csv"),
        {"audience": audience.slug, "client": api_client_record.slug, "suppression": "any"},
        **auth_headers(),
    )

    assert response.status_code == 200
    csv_text = response.content.decode()
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert rows[0]["email"] == "safe@example.com"
    assert rows[0]["tags"] == "newsletter"
    assert rows[0]["subscription_status"] == SubscriptionStatus.UNSUBSCRIBED
    assert rows[0]["email_validation_status"] == EmailValidationStatus.MANUALLY_INVALID
    serialized = csv_text.lower()
    assert "api_key" not in serialized
    assert "hash" not in serialized
    assert "tracking" not in serialized
    assert "unsubscribe_token" not in serialized

    output_path = tmp_path / "contacts.csv"
    call_command(
        "export_contacts_csv",
        "--organization",
        audience.organization.slug,
        "--audience",
        audience.slug,
        "--client",
        api_client_record.slug,
        "--output",
        str(output_path),
    )
    command_rows = list(csv.DictReader(io.StringIO(output_path.read_text(encoding="utf-8"))))
    assert command_rows[0]["email"] == "safe@example.com"


def test_bulk_api_errors_are_bearer_only_json_without_redirects(client, api_client_record):
    token_response = client.post(
        reverse("mailing:api_contact_imports"),
        data={},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Token {API_KEY}",
    )
    assert token_response.status_code == 401
    assert token_response.headers["Content-Type"].startswith("application/json")
    assert token_response.json()["error"]["code"] == "invalid_authorization"

    method_response = client.delete(reverse("mailing:api_contacts"), follow=False)
    assert method_response.status_code == 405
    assert method_response.headers["Content-Type"].startswith("application/json")
    assert "Location" not in method_response.headers


def create_contact(
    email,
    audience,
    client,
    *,
    tags=None,
    status=SubscriptionStatus.PENDING,
    verified=False,
    validation_status=EmailValidationStatus.UNKNOWN,
    global_unsubscribed=False,
    hard_bounced=False,
):
    now = timezone.now()
    contact = Contact.objects.create(
        email=email,
        verified_at=now if verified else None,
        email_validation_status=validation_status,
        email_validated_at=now if validation_status != EmailValidationStatus.UNKNOWN else None,
        global_unsubscribed_at=now if global_unsubscribed else None,
        hard_bounced_at=now if hard_bounced else None,
    )
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=client,
        status=status,
        verified_at=now if verified else None,
        unsubscribed_at=now if status == SubscriptionStatus.UNSUBSCRIBED else None,
        unsubscribe_reason="api" if status == SubscriptionStatus.UNSUBSCRIBED else "",
    )
    for tag_name in tags or []:
        tag, _ = Tag.objects.get_or_create(audience=audience, slug=tag_name, defaults={"name": tag_name})
        ContactTag.objects.create(contact=contact, tag=tag)
    return contact
