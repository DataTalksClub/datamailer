import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import (
    Audience,
    Campaign,
    Client,
    ClientApiKey,
    Contact,
    ContactTag,
    EmailValidationStatus,
    OperatorAudit,
    Organization,
    Subscription,
    SubscriptionStatus,
    Tag,
)
from mailing.services.auth import authenticate_bearer_token, check_api_key, create_client_api_key
from mailing.services.campaigns import estimate_campaign_recipients, snapshot_campaign_recipients

pytestmark = pytest.mark.django_db


def select_active_client(django_client, client_record):
    session = django_client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = client_record.id
    session.save()


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
    tag = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    routes = [
        reverse("mailing:client_list"),
        reverse("mailing:client_detail", args=[client_record.id]),
        reverse("mailing:audience_create"),
        reverse("mailing:audience_edit", args=[audience.id]),
        reverse("mailing:tag_create", args=[audience.id]),
        reverse("mailing:tag_edit", args=[tag.id]),
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


def test_client_list_renders_active_api_key_count(client, operator, audience, client_record):
    active_one, _ = create_client_api_key(client=client_record, name="Website")
    active_two, _ = create_client_api_key(client=client_record, name="Course platform")
    active_one.revoked_at = timezone.now()
    active_one.save(update_fields=["revoked_at", "updated_at"])
    active_two.last_used_at = timezone.now()
    active_two.save(update_fields=["last_used_at", "updated_at"])
    assert client_record.active_api_key_count == 1
    assert not hasattr(audience, "active_api_key_count")
    client.force_login(operator)

    response = client.get(reverse("mailing:client_list"))
    page = response.content.decode()

    assert response.status_code == 200
    assert "API keys" in page
    assert '<span class="badge success">1 active</span>' in page
    assert '<span class="badge neutral">1 revoked</span>' in page
    assert "Never" not in page


def test_client_list_key_counts_do_not_bleed_between_clients(client, operator, client_record):
    other_organization = Organization.objects.create(name="Other Org", slug="other-org")
    other_client = Client.objects.create(organization=other_organization, name="Other", slug="other")
    create_client_api_key(client=client_record, name="Website")
    create_client_api_key(client=other_client, name="Other website")
    create_client_api_key(client=other_client, name="Other course")
    client.force_login(operator)

    response = client.get(reverse("mailing:client_list"))
    page = response.content.decode()

    own_row = page[page.index(f'href="{reverse("mailing:client_detail", args=[client_record.id])}"') :]
    own_row = own_row[: own_row.index("</tr>")]
    other_row = page[page.index(f'href="{reverse("mailing:client_detail", args=[other_client.id])}"') :]
    other_row = other_row[: other_row.index("</tr>")]
    assert '<span class="badge success">1 active</span>' in own_row
    assert '<span class="badge success">2 active</span>' in other_row


def test_client_detail_renders_identity_status_and_api_key_rows(client, operator, client_record):
    active_key, _ = create_client_api_key(client=client_record, name="Website")
    active_key.notes = "Used by public signup."
    active_key.last_used_at = timezone.now()
    active_key.save(update_fields=["notes", "last_used_at", "updated_at"])
    revoked_key, _ = create_client_api_key(client=client_record, name="Old script")
    revoked_key.revoked_at = timezone.now()
    revoked_key.save(update_fields=["revoked_at", "updated_at"])
    client.force_login(operator)

    response = client.get(reverse("mailing:client_detail", args=[client_record.id]))
    page = response.content.decode()

    assert response.status_code == 200
    assert "Integration summary" in page
    assert "<code>dtc</code>" in page
    assert '<span class="badge success">1 active API keys</span>' in page
    assert '<span class="badge neutral">1 revoked</span>' in page
    assert "Key and purpose" in page
    assert "Safe prefix" in page
    assert "Used by public signup." in page
    assert active_key.display_prefix in page
    assert revoked_key.display_prefix in page
    assert '<span class="badge danger">Revoked</span>' in page
    assert page.count("Revoke key") == 1
    assert reverse("mailing:client_api_key_revoke", args=[client_record.id, active_key.id]) in page
    assert reverse("mailing:client_api_key_revoke", args=[client_record.id, revoked_key.id]) not in page
    assert active_key.key_hash not in page
    assert revoked_key.key_hash not in page


def test_client_api_keys_create_list_revoke_one_key_and_auth_safe(client, operator, client_record):
    client.force_login(operator)

    generated = client.post(
        reverse("mailing:client_api_key_create", args=[client_record.id]),
        {"name": "Website registration", "notes": "Used by the signup form."},
        follow=True,
    )
    raw_key = generated.context["raw_api_key_context"]["raw_key"]
    api_key = ClientApiKey.objects.get(client=client_record, name="Website registration")
    assert raw_key.startswith("dm_")
    assert raw_key in generated.content.decode()
    assert api_key.display_prefix in generated.content.decode()
    assert api_key.key_hash
    assert raw_key != api_key.key_hash
    assert check_api_key(raw_key, api_key.key_hash) is True
    assert authenticate_bearer_token(f"Bearer {raw_key}").client == client_record
    assert raw_key not in str(OperatorAudit.objects.latest("id").metadata)

    later_get = client.get(reverse("mailing:client_detail", args=[client_record.id]))
    assert raw_key not in later_get.content.decode()
    assert api_key.key_hash not in later_get.content.decode()
    assert b"Website registration" in later_get.content
    assert b"Used by the signup form." in later_get.content

    second = client.post(
        reverse("mailing:client_api_key_create", args=[client_record.id]),
        {"name": "Course platform"},
        follow=True,
    )
    second_raw_key = second.context["raw_api_key_context"]["raw_key"]
    second_key = ClientApiKey.objects.get(client=client_record, name="Course platform")
    assert second_raw_key != raw_key
    assert authenticate_bearer_token(f"Bearer {second_raw_key}").client == client_record

    client.post(reverse("mailing:client_api_key_revoke", args=[client_record.id, api_key.id]))
    api_key.refresh_from_db()
    second_key.refresh_from_db()
    assert api_key.revoked_at is not None
    assert second_key.revoked_at is None
    assert authenticate_bearer_token(f"Bearer {raw_key}").error == "invalid_api_key"
    assert authenticate_bearer_token(f"Bearer {second_raw_key}").client == client_record


def test_raw_api_key_reveal_is_scoped_to_created_key_client(client, operator, client_record):
    other_organization = Organization.objects.create(name="Other Org", slug="other-org")
    other_client = Client.objects.create(organization=other_organization, name="Other", slug="other")
    client.force_login(operator)

    generated = client.post(
        reverse("mailing:client_api_key_create", args=[client_record.id]),
        {"name": "Website registration"},
    )
    assert generated.status_code == 302
    api_key = ClientApiKey.objects.get(client=client_record, name="Website registration")
    session_key = client.session["operator_raw_api_key"]
    raw_key = session_key["raw_key"]

    other_detail = client.get(reverse("mailing:client_detail", args=[other_client.id]))
    assert raw_key not in other_detail.content.decode()

    own_detail = client.get(reverse("mailing:client_detail", args=[client_record.id]))
    assert raw_key in own_detail.content.decode()
    assert api_key.key_hash not in own_detail.content.decode()


def test_inactive_client_rejects_otherwise_valid_key(client, operator, client_record):
    client.force_login(operator)
    response = client.post(
        reverse("mailing:client_api_key_create", args=[client_record.id]),
        {"name": "Inactive test"},
        follow=True,
    )
    raw_key = response.context["raw_api_key_context"]["raw_key"]
    client_record.is_active = False
    client_record.save(update_fields=["is_active", "updated_at"])

    assert authenticate_bearer_token(f"Bearer {raw_key}").error == "inactive_client"


def test_audience_and_tag_forms_validate_duplicate_slugs(client, operator, organization, audience, client_record):
    client.force_login(operator)
    duplicate_audience = client.post(
        reverse("mailing:audience_create"),
        {"organization": organization.id, "name": "Duplicate", "slug": audience.slug},
    )
    duplicate_audience_html = duplicate_audience.content.decode()
    assert duplicate_audience.status_code == 200
    assert b"Audience slug must be unique" in duplicate_audience.content
    assert '<div class="field-errors"><ul class="errorlist" id="id_slug_error">' in duplicate_audience_html

    Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    duplicate_tag = client.post(
        reverse("mailing:tag_create", args=[audience.id]),
        {"name": "Newsletter again", "slug": "newsletter"},
    )
    duplicate_tag_html = duplicate_tag.content.decode()
    assert duplicate_tag.status_code == 200
    assert b"Tag slug must be unique" in duplicate_tag.content
    assert '<div class="field-errors"><ul class="errorlist" id="id_slug_error">' in duplicate_tag_html


def test_audience_and_tag_form_success_redirects_are_preserved(client, operator, organization, audience, client_record):
    client.force_login(operator)

    created_audience_response = client.post(
        reverse("mailing:audience_create"),
        {"organization": organization.id, "name": "Course Alumni", "slug": "course-alumni"},
    )
    created_audience = Audience.objects.get(slug="course-alumni")
    assert created_audience_response.status_code == 302
    assert created_audience_response["Location"] == reverse("mailing:audience_detail", args=[created_audience.id])

    edit_audience_response = client.post(
        reverse("mailing:audience_edit", args=[created_audience.id]),
        {"organization": organization.id, "name": "Course Alumni Updated", "slug": "course-alumni"},
    )
    assert edit_audience_response.status_code == 302
    assert edit_audience_response["Location"] == reverse("mailing:audience_detail", args=[created_audience.id])

    created_tag_response = client.post(
        reverse("mailing:tag_create", args=[audience.id]),
        {"name": "Newsletter", "slug": "newsletter"},
    )
    created_tag = Tag.objects.get(audience=audience, slug="newsletter")
    assert created_tag_response.status_code == 302
    assert created_tag_response["Location"] == reverse("mailing:tag_detail", args=[created_tag.id])

    edit_tag_response = client.post(
        reverse("mailing:tag_edit", args=[created_tag.id]),
        {"name": "Newsletter Updated", "slug": "newsletter"},
    )
    assert edit_tag_response.status_code == 302
    assert edit_tag_response["Location"] == reverse("mailing:tag_detail", args=[created_tag.id])


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
    client.post(reverse("mailing:contact_state_update", args=[contact.normalized_email]), state_payload)
    client.post(reverse("mailing:contact_state_update", args=[contact.normalized_email]), state_payload)
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
    client.post(reverse("mailing:contact_subscription_update", args=[contact.normalized_email]), subscription_payload)
    client.post(reverse("mailing:contact_subscription_update", args=[contact.normalized_email]), subscription_payload)
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
    client.post(reverse("mailing:contact_tag_add", args=[contact.normalized_email]), payload)
    client.post(reverse("mailing:contact_tag_add", args=[contact.normalized_email]), payload)
    assert ContactTag.objects.filter(contact=contact, tag=tag).count() == 1
    assert OperatorAudit.objects.filter(action="contact.tag.add", target_id=contact.id).count() == 1
    assert estimate_campaign_recipients(campaign).recipient_count == 1
    assert snapshot_campaign_recipients(campaign).recipient_count == 1

    membership = ContactTag.objects.get(contact=contact, tag=tag)
    client.post(reverse("mailing:contact_tag_remove", args=[contact.normalized_email]), {"membership": membership.id})
    client.post(reverse("mailing:contact_tag_remove", args=[contact.normalized_email]), {"membership": membership.id})
    assert ContactTag.objects.filter(contact=contact, tag=tag).count() == 0
    assert OperatorAudit.objects.filter(action="contact.tag.remove", target_id=contact.id).count() == 1


def test_contact_tag_form_rejects_cross_audience_tag(client, operator, organization, audience, client_record):
    other_audience = Audience.objects.create(organization=organization, name="Other", slug="other")
    tag = Tag.objects.create(audience=other_audience, name="Other tag", slug="other-tag")
    contact = create_contact("person@example.com")
    client.force_login(operator)

    response = client.post(
        reverse("mailing:contact_tag_add", args=[contact.normalized_email]),
        {"audience": audience.id, "tag": tag.id, "new_tag_name": "", "new_tag_slug": ""},
        follow=True,
    )

    assert response.status_code == 200
    assert ContactTag.objects.filter(contact=contact).count() == 0
    assert b"Tag was not added" in response.content
