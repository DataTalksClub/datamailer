import pytest
from django.contrib import admin
from django.db import IntegrityError, transaction
from django.utils import timezone

from mailing.admin import CampaignAdmin, CampaignRecipientAdmin
from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    Client,
    Contact,
    ContactTag,
    Organization,
    Subscription,
    SubscriptionStatus,
    Tag,
)
from mailing.services import snapshot_campaign_recipients

pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def client(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def campaign(audience, client):
    return Campaign.objects.create(
        audience=audience,
        client=client,
        subject="Weekly update",
        preview_text="A short preview",
        html_body="<p>Hello</p>",
        text_body="Hello",
        include_tags=[" python ", "ml", "python"],
        exclude_tags=["inactive", "INACTIVE"],
    )


def create_contact(email, audience, client, *, verified=True, status=SubscriptionStatus.SUBSCRIBED, **contact_fields):
    contact = Contact.objects.create(
        email=email,
        verified_at=timezone.now() if verified else None,
        **contact_fields,
    )
    Subscription.objects.create(contact=contact, audience=audience, client=client, status=status)
    return contact


def test_campaign_filters_are_stored_deterministically(campaign):
    campaign.refresh_from_db()

    assert campaign.include_tags == ["ml", "python"]
    assert campaign.exclude_tags == ["inactive"]


def test_campaign_recipient_constraints_and_token_hash_uniqueness(campaign, audience, client):
    contact = create_contact("person@example.com", audience, client)
    CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email)

    second_contact = create_contact("other@example.com", audience, client)
    CampaignRecipient.objects.create(
        campaign=campaign,
        contact=second_contact,
        email=second_contact.email,
        tracking_token_hash="tracking-hash",
        unsubscribe_token_hash="unsubscribe-hash",
    )
    third_contact = create_contact("third@example.com", audience, client)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CampaignRecipient.objects.create(
                campaign=campaign,
                contact=third_contact,
                email=third_contact.email,
                tracking_token_hash="tracking-hash",
            )


def test_snapshot_creates_pending_recipients_for_verified_subscribed_contacts(campaign, audience, client):
    eligible = create_contact("eligible@example.com", audience, client)
    tag = Tag.objects.create(audience=audience, name="Python", slug="python")
    second_tag = Tag.objects.create(audience=audience, name="ML", slug="ml")
    ContactTag.objects.create(contact=eligible, tag=tag)
    ContactTag.objects.create(contact=eligible, tag=second_tag)

    result = snapshot_campaign_recipients(campaign)

    recipient = CampaignRecipient.objects.get(campaign=campaign, contact=eligible)
    assert result.created_count == 1
    assert recipient.email == "eligible@example.com"
    assert recipient.status == CampaignRecipientStatus.PENDING
    assert recipient.skip_reason == ""
    campaign.refresh_from_db()
    assert campaign.recipient_count == 1
    assert campaign.skipped_count == 0
    assert campaign.sent_count == 0
    assert campaign.open_count == 0
    assert campaign.click_count == 0


def test_snapshot_applies_include_and_exclude_tag_filters(campaign, audience, client):
    python = Tag.objects.create(audience=audience, name="Python", slug="python")
    ml = Tag.objects.create(audience=audience, name="ML", slug="ml")
    inactive = Tag.objects.create(audience=audience, name="Inactive", slug="inactive")
    included = create_contact("included@example.com", audience, client)
    missing_include = create_contact("missing@example.com", audience, client)
    excluded = create_contact("excluded@example.com", audience, client)

    for contact in (included, excluded):
        ContactTag.objects.create(contact=contact, tag=python)
        ContactTag.objects.create(contact=contact, tag=ml)
    ContactTag.objects.create(contact=missing_include, tag=python)
    ContactTag.objects.create(contact=excluded, tag=inactive)

    snapshot_campaign_recipients(campaign)

    assert list(CampaignRecipient.objects.values_list("email", flat=True)) == ["included@example.com"]


@pytest.mark.parametrize(
    ("email", "contact_kwargs", "subscription_status", "audience_status", "expected_reason"),
    [
        ("unverified@example.com", {"verified": False}, SubscriptionStatus.SUBSCRIBED, None, "unverified"),
        (
            "global@example.com",
            {"global_unsubscribed_at": timezone.now()},
            SubscriptionStatus.SUBSCRIBED,
            None,
            "global_unsubscribe",
        ),
        (
            "client@example.com",
            {},
            SubscriptionStatus.UNSUBSCRIBED,
            None,
            "client_unsubscribe",
        ),
        (
            "audience@example.com",
            {},
            SubscriptionStatus.SUBSCRIBED,
            SubscriptionStatus.UNSUBSCRIBED,
            "audience_unsubscribe",
        ),
        (
            "bounce@example.com",
            {"hard_bounced_at": timezone.now()},
            SubscriptionStatus.SUBSCRIBED,
            None,
            "hard_bounce",
        ),
        (
            "complaint@example.com",
            {"complained_at": timezone.now()},
            SubscriptionStatus.SUBSCRIBED,
            None,
            "complaint",
        ),
        ("suppressed@example.com", {}, SubscriptionStatus.PENDING, None, "suppressed"),
    ],
)
def test_snapshot_records_explicit_skip_reasons(
    audience,
    client,
    email,
    contact_kwargs,
    subscription_status,
    audience_status,
    expected_reason,
):
    campaign = Campaign.objects.create(audience=audience, client=client, subject=f"Campaign for {expected_reason}")
    contact = create_contact(email, audience, client, status=subscription_status, **contact_kwargs)
    if audience_status:
        Subscription.objects.create(contact=contact, audience=audience, status=audience_status)

    snapshot_campaign_recipients(campaign)

    recipient = CampaignRecipient.objects.get(campaign=campaign, contact=contact)
    assert recipient.status == CampaignRecipientStatus.SKIPPED
    assert recipient.skip_reason == expected_reason
    campaign.refresh_from_db()
    assert campaign.recipient_count == 0
    assert campaign.skipped_count == 1


def test_duplicate_skip_reason_is_available_for_imported_or_manually_classified_rows():
    assert CampaignRecipientSkipReason.DUPLICATE == "duplicate"
    assert "duplicate" in {choice for choice, _label in CampaignRecipientSkipReason.choices}


def test_snapshot_is_idempotent_and_does_not_rewrite_existing_rows(campaign, audience, client):
    campaign.include_tags = []
    campaign.exclude_tags = []
    campaign.save()
    contact = create_contact("person@example.com", audience, client)

    first = snapshot_campaign_recipients(campaign)
    recipient = CampaignRecipient.objects.get(campaign=campaign, contact=contact)
    recipient.email = "snapshotted@example.com"
    recipient.status = CampaignRecipientStatus.SKIPPED
    recipient.skip_reason = CampaignRecipientSkipReason.DUPLICATE
    recipient.save()
    contact.email = "changed@example.com"
    contact.global_unsubscribed_at = timezone.now()
    contact.save()
    second = snapshot_campaign_recipients(campaign)

    recipient.refresh_from_db()
    campaign.refresh_from_db()
    assert first.created_count == 1
    assert second.created_count == 0
    assert CampaignRecipient.objects.count() == 1
    assert recipient.email == "snapshotted@example.com"
    assert recipient.status == CampaignRecipientStatus.SKIPPED
    assert recipient.skip_reason == CampaignRecipientSkipReason.DUPLICATE
    assert campaign.recipient_count == 0
    assert campaign.skipped_count == 1


def test_snapshot_does_not_delete_rows_after_later_tag_changes(campaign, audience, client):
    python = Tag.objects.create(audience=audience, name="Python", slug="python")
    ml = Tag.objects.create(audience=audience, name="ML", slug="ml")
    contact = create_contact("person@example.com", audience, client)
    python_membership = ContactTag.objects.create(contact=contact, tag=python)
    ContactTag.objects.create(contact=contact, tag=ml)

    snapshot_campaign_recipients(campaign)
    python_membership.delete()
    snapshot_campaign_recipients(campaign)

    assert CampaignRecipient.objects.filter(campaign=campaign, contact=contact).count() == 1


def test_admin_registers_campaign_and_recipient_search_filters():
    campaign_admin = admin.site._registry[Campaign]
    recipient_admin = admin.site._registry[CampaignRecipient]

    assert isinstance(campaign_admin, CampaignAdmin)
    assert "status" in campaign_admin.list_filter
    assert "client" in campaign_admin.list_filter
    assert "subject" in campaign_admin.search_fields
    assert isinstance(recipient_admin, CampaignRecipientAdmin)
    assert "status" in recipient_admin.list_filter
    assert "skip_reason" in recipient_admin.list_filter
    assert "campaign__subject" in recipient_admin.search_fields
    assert "contact__normalized_email" in recipient_admin.search_fields
