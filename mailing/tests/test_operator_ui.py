import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    Contact,
    ContactTag,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    Subscription,
    SubscriptionStatus,
    Tag,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.operator_ui import (
    campaign_recipient_queryset,
    campaign_stats,
    contact_event_timeline,
    contact_search_queryset,
    metadata_summary,
    rate,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator():
    return get_user_model().objects.create_user("operator", "operator@example.com", "password", is_staff=True)


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def client_record(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def campaign(audience, client_record):
    return Campaign.objects.create(
        audience=audience,
        client=client_record,
        subject="Weekly update",
        status="sent",
        scheduled_at=timezone.now(),
        sent_at=timezone.now(),
        recipient_count=4,
        sent_count=3,
        skipped_count=1,
        delivered_count=2,
        unique_open_count=1,
        open_count=3,
        unique_click_count=1,
        click_count=2,
        unsubscribe_count=1,
        bounce_count=1,
        complaint_count=1,
    )


def create_contact(email="person@example.com", **kwargs):
    return Contact.objects.create(email=email, **kwargs)


def create_recipient(campaign, email, *, status=CampaignRecipientStatus.SENT, **kwargs):
    contact = create_contact(email)
    return CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=email, status=status, **kwargs)


def test_rate_hides_unavailable_denominators():
    assert rate(1, 4) == "25.0%"
    assert rate(1, 0) == ""


def test_campaign_stats_include_derived_rates_and_failed_count(campaign):
    create_recipient(campaign, "failed@example.com", status=CampaignRecipientStatus.FAILED)

    stats = {stat.key: stat for stat in campaign_stats(campaign)}

    assert stats["sent"].value == 3
    assert stats["sent"].rate == "75.0%"
    assert stats["delivered"].rate == "66.7%"
    assert stats["failures"].value == 1
    assert stats["failures"].rate == "25.0%"


def test_campaign_recipient_filter_mapping(campaign):
    opened = create_recipient(campaign, "opened@example.com", first_opened_at=timezone.now())
    create_recipient(campaign, "not-opened@example.com")
    bounced = create_recipient(campaign, "bounced@example.com", status=CampaignRecipientStatus.BOUNCED)

    assert list(campaign_recipient_queryset(campaign, "opened")) == [opened]
    assert bounced in list(campaign_recipient_queryset(campaign, "bounced"))
    assert opened not in list(campaign_recipient_queryset(campaign, "not_opened"))


def test_contact_search_uses_normalized_email(audience, client_record):
    contact = create_contact("Person@Example.COM")
    Subscription.objects.create(contact=contact, audience=audience, client=client_record)

    assert list(contact_search_queryset(" person@example.com ")) == [contact]


def test_contact_timeline_is_newest_first_and_metadata_is_operator_readable(campaign, client_record, audience):
    contact = create_contact("person@example.com")
    older = EmailEvent.objects.create(
        contact=contact,
        client=client_record,
        audience=audience,
        campaign=campaign,
        event_type=EmailEventType.QUEUED,
        metadata={"reason": "snapshot"},
    )
    newer = EmailEvent.objects.create(
        contact=contact,
        client=client_record,
        audience=audience,
        campaign=campaign,
        event_type=EmailEventType.CLICK,
        url="https://example.com",
        metadata={"scope": "campaign", "ignored": "value"},
    )

    assert list(contact_event_timeline(contact)) == [newer, older]
    assert metadata_summary(newer.metadata) == "scope: campaign"


def test_operator_campaign_list_requires_staff(client):
    response = client.get(reverse("mailing:operator_campaign_list"))

    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


def test_operator_campaign_list_renders_recent_campaigns(client, operator, campaign):
    client.force_login(operator)

    response = client.get(reverse("mailing:operator_campaign_list"))

    assert response.status_code == 200
    assert b"Weekly update" in response.content
    assert b"DTC Courses" in response.content
    assert b"DataTalksClub" in response.content
    assert b"1 / 3" in response.content


def test_campaign_detail_renders_stats_and_recipient_audit_fields(client, operator, campaign):
    client.force_login(operator)
    recipient = create_recipient(
        campaign,
        "person@example.com",
        first_opened_at=timezone.now(),
        first_clicked_at=timezone.now(),
        open_count=2,
        click_count=1,
        ses_message_id="ses-123",
        last_error="",
    )
    create_recipient(campaign, "other@example.com")

    response = client.get(reverse("mailing:operator_campaign_detail", args=[campaign.id]), {"filter": "opened"})

    assert response.status_code == 200
    assert b"Unique opens" in response.content
    assert b"33.3%" in response.content
    assert recipient.email.encode() in response.content
    assert b"other@example.com" not in response.content
    assert b"ses-123" in response.content
    assert f'id="recipient-{recipient.id}"'.encode() in response.content


def test_campaign_detail_paginates_recipients(client, operator, campaign):
    client.force_login(operator)
    for index in range(55):
        create_recipient(campaign, f"person-{index:02d}@example.com")

    response = client.get(reverse("mailing:operator_campaign_detail", args=[campaign.id]))

    assert response.status_code == 200
    assert b"person-00@example.com" in response.content
    assert b"person-54@example.com" not in response.content
    assert b"Page 1 of 2" in response.content


def test_contact_search_and_detail_render_operator_context(client, operator, audience, client_record, campaign):
    client.force_login(operator)
    contact = create_contact(
        "Person@Example.COM",
        verified_at=timezone.now(),
        global_unsubscribed_at=timezone.now(),
        hard_bounced_at=timezone.now(),
        complained_at=timezone.now(),
    )
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=client_record,
        status=SubscriptionStatus.UNSUBSCRIBED,
        unsubscribe_reason="requested",
    )
    tag = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    ContactTag.objects.create(contact=contact, tag=tag)
    recipient = CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.UNSUBSCRIBED,
        sent_at=timezone.now(),
    )
    template = EmailTemplate.objects.create(
        client=client_record,
        key="welcome",
        name="Welcome",
        subject="Welcome",
        is_transactional=True,
    )
    TransactionalMessage.objects.create(
        client=client_record,
        contact=contact,
        email=contact.email,
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.SENT,
        subject="Welcome",
        sent_at=timezone.now(),
    )
    EmailEvent.objects.create(
        contact=contact,
        client=client_record,
        audience=audience,
        campaign=campaign,
        campaign_recipient=recipient,
        event_type=EmailEventType.UNSUBSCRIBE,
        metadata={"scope": "global"},
    )

    search_response = client.get(reverse("mailing:operator_contact_search"), {"q": "person@example.com"})
    detail_response = client.get(reverse("mailing:operator_contact_detail", args=[contact.id]))

    assert search_response.status_code == 200
    assert b"Person@Example.COM" in search_response.content
    assert detail_response.status_code == 200
    assert b"Global unsubscribe" in detail_response.content
    assert b"requested" in detail_response.content
    assert b"Newsletter" in detail_response.content
    assert f"/operator/campaigns/{campaign.id}/#recipient-{recipient.id}".encode() in detail_response.content
    assert b"Transactional Messages" in detail_response.content
    assert b"Unsubscribe" in detail_response.content
    assert b"scope: global" in detail_response.content


def test_contact_detail_paginates_events(client, operator, campaign, client_record, audience):
    client.force_login(operator)
    contact = create_contact("person@example.com")
    for index in range(55):
        EmailEvent.objects.create(
            contact=contact,
            campaign=campaign,
            client=client_record,
            audience=audience,
            event_type=EmailEventType.OPEN,
            metadata={"index": index},
        )

    response = client.get(reverse("mailing:operator_contact_detail", args=[contact.id]))

    assert response.status_code == 200
    assert b"Page 1 of 2" in response.content
    assert response.content.count(b"Open") == 50
