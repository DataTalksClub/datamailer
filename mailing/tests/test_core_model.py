import pytest
from django.contrib import admin
from django.db import IntegrityError
from django.utils import timezone

from mailing.admin import ContactAdmin, SubscriptionAdmin, TagAdmin
from mailing.models import Audience, Client, Contact, ContactTag, Organization, Subscription, SubscriptionStatus, Tag
from mailing.services import (
    assign_tag,
    get_contact_suppression_state,
    get_subscription_for_slugs,
    is_marketing_email_allowed,
    is_transactional_email_allowed,
    normalize_email,
    subscribe_contact,
    unsubscribe_contact,
    upsert_contact,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def second_audience(organization):
    return Audience.objects.create(organization=organization, name="AI Shipping Labs", slug="ai-shipping-labs")


@pytest.fixture
def client(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def second_client(organization):
    return Client.objects.create(organization=organization, name="DTC Newsletter", slug="dtc-newsletter")


def test_normalize_email_is_deterministic():
    assert normalize_email("  PERSON+List@Example.COM ") == "person+list@example.com"


def test_contact_save_normalizes_email_before_persistence():
    contact = Contact.objects.create(email=" Person@Example.COM ")

    assert contact.email == "Person@Example.COM"
    assert contact.normalized_email == "person@example.com"


def test_normalized_email_is_unique():
    Contact.objects.create(email="person@example.com")

    with pytest.raises(IntegrityError):
        Contact.objects.create(email=" PERSON@example.com ")


def test_upsert_contact_uses_normalized_email_for_lookup():
    first_contact, created = upsert_contact("Person@Example.COM")
    second_contact, second_created = upsert_contact(" person@example.com ")

    assert created is True
    assert second_created is False
    assert second_contact.id == first_contact.id
    assert Contact.objects.count() == 1
    assert second_contact.email == "person@example.com"
    assert second_contact.normalized_email == "person@example.com"


def test_one_contact_can_have_multiple_audience_and_client_subscriptions(
    audience,
    second_audience,
    client,
    second_client,
):
    contact = Contact.objects.create(email="person@example.com")

    Subscription.objects.create(contact=contact, audience=audience, client=client, status=SubscriptionStatus.SUBSCRIBED)
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=second_client,
        status=SubscriptionStatus.UNSUBSCRIBED,
    )
    Subscription.objects.create(
        contact=contact,
        audience=second_audience,
        client=client,
        status=SubscriptionStatus.PENDING,
    )

    assert Contact.objects.count() == 1
    assert set(contact.subscriptions.values_list("audience__slug", "client__slug", "status")) == {
        ("datatalks-club", "dtc-courses", SubscriptionStatus.SUBSCRIBED),
        ("datatalks-club", "dtc-newsletter", SubscriptionStatus.UNSUBSCRIBED),
        ("ai-shipping-labs", "dtc-courses", SubscriptionStatus.PENDING),
    }


def test_subscription_scope_is_unique_for_client_scoped_rows(audience, client):
    contact = Contact.objects.create(email="person@example.com")
    Subscription.objects.create(contact=contact, audience=audience, client=client)

    with pytest.raises(IntegrityError):
        Subscription.objects.create(contact=contact, audience=audience, client=client)


def test_audience_only_subscription_rows_are_supported_and_unique(audience, client):
    contact = Contact.objects.create(email="person@example.com")
    audience_subscription = Subscription.objects.create(
        contact=contact,
        audience=audience,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    client_subscription = Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=client,
        status=SubscriptionStatus.UNSUBSCRIBED,
    )

    assert audience_subscription.client is None
    assert client_subscription.client_id == client.id
    assert Subscription.objects.count() == 2

    with pytest.raises(IntegrityError):
        Subscription.objects.create(contact=contact, audience=audience, status=SubscriptionStatus.PENDING)


def test_subscribe_and_unsubscribe_are_idempotent_transitions(audience, client):
    contact = Contact.objects.create(email="person@example.com")

    subscribed = subscribe_contact(contact, audience, client)
    subscribed_again = subscribe_contact(contact, audience, client)
    unsubscribed = unsubscribe_contact(contact, audience, client, reason="user request")

    assert subscribed_again.id == subscribed.id == unsubscribed.id
    assert Subscription.objects.count() == 1
    assert unsubscribed.status == SubscriptionStatus.UNSUBSCRIBED
    assert unsubscribed.unsubscribed_at is not None
    assert unsubscribed.unsubscribe_reason == "user request"


def test_subscribe_and_lookup_support_audience_only_scope(audience):
    contact = Contact.objects.create(email="person@example.com")

    subscription = subscribe_contact(contact, audience)
    fetched = get_subscription_for_slugs("PERSON@example.com", audience.slug)
    unsubscribed = unsubscribe_contact(contact, audience, reason="audience unsubscribe")

    assert subscription.client is None
    assert fetched.id == subscription.id
    assert unsubscribed.id == subscription.id
    assert unsubscribed.status == SubscriptionStatus.UNSUBSCRIBED
    assert unsubscribed.unsubscribe_reason == "audience unsubscribe"


def test_tags_are_audience_scoped_and_membership_is_idempotent(audience, second_audience):
    contact = Contact.objects.create(email="person@example.com")

    membership = assign_tag(contact, audience, "Course: ML Zoomcamp")
    second_membership = assign_tag(contact, audience, "Course: ML Zoomcamp")
    other_audience_membership = assign_tag(contact, second_audience, "Course: ML Zoomcamp")

    assert second_membership.id == membership.id
    assert ContactTag.objects.count() == 2
    assert membership.tag.slug == "course-ml-zoomcamp"
    assert other_audience_membership.tag.slug == "course-ml-zoomcamp"
    assert membership.tag.audience_id != other_audience_membership.tag.audience_id


def test_duplicate_contact_tag_membership_is_rejected(audience):
    contact = Contact.objects.create(email="person@example.com")
    tag = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    ContactTag.objects.create(contact=contact, tag=tag)

    with pytest.raises(IntegrityError):
        ContactTag.objects.create(contact=contact, tag=tag)


def test_global_suppression_is_separate_from_subscription_status(audience, client):
    contact = Contact.objects.create(email="person@example.com", global_unsubscribed_at=timezone.now())
    subscription = Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=client,
        status=SubscriptionStatus.SUBSCRIBED,
    )

    suppression = get_contact_suppression_state(contact)

    assert subscription.status == SubscriptionStatus.SUBSCRIBED
    assert suppression.is_globally_unsubscribed is True
    assert suppression.has_marketing_suppression is True
    assert suppression.has_hard_suppression is False
    assert is_marketing_email_allowed(contact, audience, client) is False
    assert is_transactional_email_allowed(contact) is True


def test_hard_bounce_or_complaint_blocks_transactional_eligibility(audience, client):
    hard_bounced = Contact.objects.create(email="bounce@example.com", hard_bounced_at=timezone.now())
    complained = Contact.objects.create(email="complaint@example.com", complained_at=timezone.now())
    allowed = Contact.objects.create(email="allowed@example.com")
    subscribe_contact(allowed, audience, client)

    assert is_transactional_email_allowed(hard_bounced) is False
    assert is_transactional_email_allowed(complained) is False
    assert is_marketing_email_allowed(allowed, audience, client) is True


def test_admin_registers_search_and_filters_for_core_records():
    contact_admin = admin.site._registry[Contact]
    subscription_admin = admin.site._registry[Subscription]
    tag_admin = admin.site._registry[Tag]

    assert isinstance(contact_admin, ContactAdmin)
    assert "normalized_email" in contact_admin.search_fields
    assert "subscriptions__audience__slug" in contact_admin.search_fields
    assert "global_unsubscribed_at" in contact_admin.list_filter
    assert isinstance(subscription_admin, SubscriptionAdmin)
    assert "status" in subscription_admin.list_filter
    assert "contact__normalized_email" in subscription_admin.search_fields
    assert isinstance(tag_admin, TagAdmin)
    assert "audience" in tag_admin.list_filter
