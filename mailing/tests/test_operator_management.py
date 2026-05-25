import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    Client,
    Contact,
    ContactTag,
    EmailValidationStatus,
    OperatorAudit,
    Organization,
    Subscription,
    SubscriptionStatus,
    Tag,
)
from mailing.services.auth import authenticate_bearer_token, check_api_key
from mailing.services.campaigns import estimate_campaign_recipients, snapshot_campaign_recipients

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator():
    return get_user_model().objects.create_user("operator", "operator@example.com", "password", is_staff=True)


@pytest.fixture
def nonstaff():
    return get_user_model().objects.create_user("viewer", "viewer@example.com", "password", is_staff=False)


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="Newsletter", slug="newsletter")


@pytest.fixture
def client_record(organization):
    return Client.objects.create(organization=organization, name="DTC", slug="dtc")


def create_contact(email="person@example.com", **kwargs):
    return Contact.objects.create(email=email, **kwargs)


def test_management_views_require_staff(client, nonstaff, audience, client_record):
    routes = [
        reverse("mailing:client_list"),
        reverse("mailing:client_detail", args=[client_record.id]),
        reverse("mailing:audience_create"),
        reverse("mailing:tag_create", args=[audience.id]),
    ]

    for route in routes:
        anonymous = client.get(route)
        assert anonymous.status_code == 302
        assert "/admin/login/" in anonymous["Location"]

    client.force_login(nonstaff)
    for route in routes:
        response = client.get(route)
        assert response.status_code == 302
        assert "/admin/login/" in response["Location"]


def test_staff_can_load_client_list(client, operator, client_record):
    client.force_login(operator)

    response = client.get(reverse("mailing:client_list"))

    assert response.status_code == 200
    assert b"Clients" in response.content
    assert b"DTC" in response.content
    assert b"dtc" in response.content


def test_client_api_key_generate_rotate_revoke_is_one_time_and_auth_safe(client, operator, client_record):
    client.force_login(operator)

    generated = client.post(reverse("mailing:client_api_key_generate", args=[client_record.id]), follow=True)
    client_record.refresh_from_db()
    raw_key = generated.context["raw_api_key"]
    assert raw_key.startswith("dm_")
    assert raw_key in generated.content.decode()
    assert client_record.api_key_hash
    assert raw_key != client_record.api_key_hash
    assert check_api_key(raw_key, client_record.api_key_hash) is True
    assert authenticate_bearer_token(f"Bearer {raw_key}").client == client_record
    assert raw_key not in str(OperatorAudit.objects.latest("id").metadata)

    later_get = client.get(reverse("mailing:client_detail", args=[client_record.id]))
    assert raw_key not in later_get.content.decode()

    rotated = client.post(reverse("mailing:client_api_key_generate", args=[client_record.id]), follow=True)
    new_key = rotated.context["raw_api_key"]
    client_record.refresh_from_db()
    assert new_key != raw_key
    assert authenticate_bearer_token(f"Bearer {raw_key}").error == "invalid_api_key"
    assert authenticate_bearer_token(f"Bearer {new_key}").client == client_record

    client.post(reverse("mailing:client_api_key_revoke", args=[client_record.id]))
    client_record.refresh_from_db()
    assert client_record.api_key_hash == ""
    assert authenticate_bearer_token(f"Bearer {new_key}").error == "invalid_api_key"


def test_inactive_client_rejects_otherwise_valid_key(client, operator, client_record):
    client.force_login(operator)
    response = client.post(reverse("mailing:client_api_key_generate", args=[client_record.id]), follow=True)
    raw_key = response.context["raw_api_key"]
    client_record.is_active = False
    client_record.save(update_fields=["is_active", "updated_at"])

    assert authenticate_bearer_token(f"Bearer {raw_key}").error == "inactive_client"


def test_audience_and_tag_forms_validate_duplicate_slugs(client, operator, organization, audience):
    client.force_login(operator)
    duplicate_audience = client.post(
        reverse("mailing:audience_create"),
        {"organization": organization.id, "name": "Duplicate", "slug": audience.slug},
    )
    assert duplicate_audience.status_code == 200
    assert b"Audience slug must be unique" in duplicate_audience.content

    Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    duplicate_tag = client.post(
        reverse("mailing:tag_create", args=[audience.id]),
        {"name": "Newsletter again", "slug": "newsletter"},
    )
    assert duplicate_tag.status_code == 200
    assert b"Tag slug must be unique" in duplicate_tag.content


def test_client_create_edit_validation_and_audit(client, operator, organization):
    client.force_login(operator)
    response = client.post(
        reverse("mailing:client_create"),
        {"organization": organization.id, "name": "New Client", "slug": "new-client", "is_active": "on"},
    )
    app_client = Client.objects.get(slug="new-client")
    assert response.status_code == 302
    assert OperatorAudit.objects.filter(action="client.create", target_id=app_client.id).exists()

    duplicate = client.post(
        reverse("mailing:client_create"),
        {"organization": organization.id, "name": "Dup", "slug": "new-client", "is_active": "on"},
    )
    assert duplicate.status_code == 200
    assert b"Client slug must be unique" in duplicate.content


def test_contact_state_and_subscription_mutations_are_audited_and_idempotent(client, operator, audience, client_record):
    client.force_login(operator)
    contact = create_contact("person@example.com")

    state_payload = {
        "verified_state": "verified",
        "email_validation_status": EmailValidationStatus.MANUALLY_INVALID,
        "email_validation_reason": "staff review",
        "global_unsubscribed": "on",
        "hard_bounced": "",
        "complained": "",
    }
    client.post(reverse("mailing:contact_state_update", args=[contact.id]), state_payload)
    client.post(reverse("mailing:contact_state_update", args=[contact.id]), state_payload)
    contact.refresh_from_db()
    assert contact.verified_at is not None
    assert contact.email_validation_status == EmailValidationStatus.MANUALLY_INVALID
    assert contact.global_unsubscribed_at is not None
    assert OperatorAudit.objects.filter(action="contact.state.update", target_id=contact.id).count() == 1

    subscription_payload = {
        "audience": audience.id,
        "client": client_record.id,
        "status": SubscriptionStatus.UNSUBSCRIBED,
        "verified": "on",
        "unsubscribe_reason": "manual request",
    }
    client.post(reverse("mailing:contact_subscription_update", args=[contact.id]), subscription_payload)
    client.post(reverse("mailing:contact_subscription_update", args=[contact.id]), subscription_payload)
    subscription = Subscription.objects.get(contact=contact, audience=audience, client=client_record)
    assert subscription.status == SubscriptionStatus.UNSUBSCRIBED
    assert subscription.unsubscribe_reason == "manual request"
    assert OperatorAudit.objects.filter(action="contact.subscription.update", target_id=contact.id).count() == 1


def test_contact_tag_mutations_are_idempotent_and_feed_campaign_segmentation(client, operator, audience, client_record):
    client.force_login(operator)
    contact = create_contact("tagged@example.com", verified_at=timezone.now())
    Subscription.objects.create(contact=contact, audience=audience, client=client_record, status=SubscriptionStatus.SUBSCRIBED)
    tag = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    campaign = Campaign.objects.create(
        audience=audience,
        client=client_record,
        subject="Tagged campaign",
        html_body="<p>Hello</p>",
        text_body="Hello",
        include_tags=["newsletter"],
    )

    payload = {"audience": audience.id, "tag": tag.id, "new_tag_name": "", "new_tag_slug": ""}
    client.post(reverse("mailing:contact_tag_add", args=[contact.id]), payload)
    client.post(reverse("mailing:contact_tag_add", args=[contact.id]), payload)
    assert ContactTag.objects.filter(contact=contact, tag=tag).count() == 1
    assert OperatorAudit.objects.filter(action="contact.tag.add", target_id=contact.id).count() == 1
    assert estimate_campaign_recipients(campaign).recipient_count == 1
    assert snapshot_campaign_recipients(campaign).recipient_count == 1

    membership = ContactTag.objects.get(contact=contact, tag=tag)
    client.post(reverse("mailing:contact_tag_remove", args=[contact.id]), {"membership": membership.id})
    client.post(reverse("mailing:contact_tag_remove", args=[contact.id]), {"membership": membership.id})
    assert ContactTag.objects.filter(contact=contact, tag=tag).count() == 0
    assert OperatorAudit.objects.filter(action="contact.tag.remove", target_id=contact.id).count() == 1


def test_contact_tag_form_rejects_cross_audience_tag(client, operator, organization, audience):
    other_audience = Audience.objects.create(organization=organization, name="Other", slug="other")
    tag = Tag.objects.create(audience=other_audience, name="Other tag", slug="other-tag")
    contact = create_contact("person@example.com")
    client.force_login(operator)

    response = client.post(
        reverse("mailing:contact_tag_add", args=[contact.id]),
        {"audience": audience.id, "tag": tag.id, "new_tag_name": "", "new_tag_slug": ""},
        follow=True,
    )

    assert response.status_code == 200
    assert ContactTag.objects.filter(contact=contact).count() == 0
    assert b"Tag was not added" in response.content
