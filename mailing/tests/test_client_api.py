import pytest
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
from mailing.services.auth import authenticate_bearer_token, check_api_key, hash_api_key

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
    return Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
        api_key_hash=hash_api_key(API_KEY),
    )


@pytest.fixture
def other_org():
    return Organization.objects.create(name="AI Shipping Labs", slug="ai-shipping-labs")


@pytest.fixture
def other_audience(other_org):
    return Audience.objects.create(organization=other_org, name="AI Shipping Labs", slug="datatalks-club")


@pytest.fixture
def other_client(other_org):
    return Client.objects.create(
        organization=other_org,
        name="AI Shipping Labs",
        slug="ai-shipping-labs",
        api_key_hash=hash_api_key("other-key"),
    )


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def post_json(django_client, url_name, payload, raw_key=API_KEY):
    return django_client.post(
        reverse(url_name),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def test_api_key_hash_helper_does_not_store_or_match_raw_key(api_client_record):
    assert api_client_record.api_key_hash != API_KEY
    assert check_api_key(API_KEY, api_client_record.api_key_hash) is True
    assert check_api_key("wrong", api_client_record.api_key_hash) is False


def test_authentication_rejects_missing_unknown_and_inactive_clients(client, api_client_record):
    response = client.post(reverse("mailing:api_contacts"), data={}, content_type="application/json")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_authorization"

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
