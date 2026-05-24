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
    EmailValidationStatus,
    Organization,
    Subscription,
    SubscriptionStatus,
    Tag,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.campaigns import estimate_campaign_recipients
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


def create_subscribed_contact(email, audience, client, *, verified=True, status=SubscriptionStatus.SUBSCRIBED, **kwargs):
    contact = create_contact(email, verified_at=timezone.now() if verified else None, **kwargs)
    Subscription.objects.create(contact=contact, audience=audience, client=client, status=status)
    return contact


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
    contact = create_contact(
        "Person@Example.COM",
        email_validation_status=EmailValidationStatus.NO_MX,
        email_validation_reason="domain missing MX",
        email_validated_at=timezone.now(),
    )
    Subscription.objects.create(contact=contact, audience=audience, client=client_record)

    assert list(contact_search_queryset(" person@example.com ")) == [contact]


def test_operator_contact_views_show_email_validation_status(client, operator, audience, client_record):
    client.force_login(operator)
    contact = create_contact(
        "Person@Example.COM",
        email_validation_status=EmailValidationStatus.MANUALLY_INVALID,
        email_validation_reason="operator marked bad",
        email_validated_at=timezone.now(),
    )
    Subscription.objects.create(contact=contact, audience=audience, client=client_record)

    search_response = client.get(reverse("mailing:operator_contact_search"), {"q": "person@example.com"})
    detail_response = client.get(reverse("mailing:operator_contact_detail", args=[contact.id]))

    assert search_response.status_code == 200
    assert b"Manually invalid" in search_response.content
    assert b"operator marked bad" in search_response.content
    assert detail_response.status_code == 200
    assert b"Validation" in detail_response.content
    assert b"Manually invalid" in detail_response.content
    assert b"operator marked bad" in detail_response.content


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
    assert b"Create campaign" in response.content


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


def test_operator_can_create_campaign_draft_with_tag_filters(client, operator, audience, client_record):
    client.force_login(operator)
    include_tag = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    exclude_tag = Tag.objects.create(audience=audience, name="Inactive", slug="inactive")

    response = client.post(
        reverse("mailing:operator_campaign_create"),
        {
            "audience": audience.id,
            "client": client_record.id,
            "subject": "Final subject",
            "preview_text": "Final preview",
            "html_body": "<p>Final HTML</p>",
            "text_body": "Final text",
            "include_tags": [include_tag.id],
            "exclude_tags": [exclude_tag.id],
            "scheduled_at": "2026-06-01T10:30",
        },
    )

    campaign = Campaign.objects.get(subject="Final subject")
    assert response.status_code == 302
    assert response["Location"] == reverse("mailing:operator_campaign_detail", args=[campaign.id])
    assert campaign.status == "draft"
    assert campaign.preview_text == "Final preview"
    assert campaign.html_body == "<p>Final HTML</p>"
    assert campaign.text_body == "Final text"
    assert campaign.include_tags == ["newsletter"]
    assert campaign.exclude_tags == ["inactive"]


def test_campaign_create_validation_rejects_missing_final_bodies_and_cross_org_client(client, operator, audience):
    client.force_login(operator)
    other_org = Organization.objects.create(name="Other", slug="other")
    other_client = Client.objects.create(organization=other_org, name="Other Client", slug="other-client")

    response = client.post(
        reverse("mailing:operator_campaign_create"),
        {
            "audience": audience.id,
            "client": other_client.id,
            "subject": "Incomplete",
            "html_body": "",
            "text_body": "",
        },
    )

    assert response.status_code == 200
    assert Campaign.objects.count() == 0
    assert b"Client must belong to the selected audience organization" in response.content
    assert b"Paste the final HTML body before saving" in response.content
    assert b"Paste the final text body before saving" in response.content


def test_operator_can_edit_draft_but_not_queued_send_content(client, operator, audience, client_record):
    client.force_login(operator)
    draft = Campaign.objects.create(
        audience=audience,
        client=client_record,
        subject="Draft",
        html_body="<p>Old</p>",
        text_body="Old",
    )
    response = client.post(
        reverse("mailing:operator_campaign_edit", args=[draft.id]),
        {
            "audience": audience.id,
            "client": client_record.id,
            "subject": "Updated",
            "html_body": "<p>New</p>",
            "text_body": "New",
        },
    )
    draft.refresh_from_db()
    assert response.status_code == 302
    assert draft.subject == "Updated"

    draft.status = "queued"
    draft.save()
    locked_response = client.post(
        reverse("mailing:operator_campaign_edit", args=[draft.id]),
        {
            "audience": audience.id,
            "client": client_record.id,
            "subject": "Changed after queue",
            "html_body": "<p>Changed</p>",
            "text_body": "Changed",
        },
    )
    draft.refresh_from_db()
    assert locked_response.status_code == 200
    assert draft.subject == "Updated"
    assert b"Queued or sent campaigns cannot be edited" in locked_response.content


def test_recipient_estimate_counts_tag_filters_and_skip_reasons(audience, client_record):
    campaign = Campaign.objects.create(
        audience=audience,
        client=client_record,
        subject="Estimate",
        html_body="<p>Hello</p>",
        text_body="Hello",
        include_tags=["newsletter"],
        exclude_tags=["inactive"],
    )
    newsletter = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    inactive = Tag.objects.create(audience=audience, name="Inactive", slug="inactive")
    eligible = create_subscribed_contact("eligible@example.com", audience, client_record)
    unverified = create_subscribed_contact("unverified@example.com", audience, client_record, verified=False)
    unsubscribed = create_subscribed_contact(
        "unsubscribed@example.com",
        audience,
        client_record,
        status=SubscriptionStatus.UNSUBSCRIBED,
    )
    filtered = create_subscribed_contact("filtered@example.com", audience, client_record)
    excluded = create_subscribed_contact("excluded@example.com", audience, client_record)
    for contact in (eligible, unverified, unsubscribed, excluded):
        ContactTag.objects.create(contact=contact, tag=newsletter)
    ContactTag.objects.create(contact=excluded, tag=inactive)

    estimate = estimate_campaign_recipients(campaign)

    assert estimate.total_candidates == 5
    assert estimate.tag_filtered_count == 2
    assert estimate.recipient_count == 1
    assert estimate.skipped_count == 2
    assert estimate.skip_reason_counts == {"client_unsubscribe": 1, "unverified": 1}
    assert [row.email for row in estimate.preview_rows] == [
        "eligible@example.com",
        "unsubscribed@example.com",
        "unverified@example.com",
    ]
    assert filtered.email not in [row.email for row in estimate.preview_rows]


def test_campaign_detail_shows_draft_estimate_and_state_dependent_controls(client, operator, audience, client_record):
    client.force_login(operator)
    campaign = Campaign.objects.create(
        audience=audience,
        client=client_record,
        subject="Draft estimate",
        html_body="<p>Hello</p>",
        text_body="Hello",
    )
    create_subscribed_contact("person@example.com", audience, client_record)

    draft_response = client.get(reverse("mailing:operator_campaign_detail", args=[campaign.id]))

    assert draft_response.status_code == 200
    assert b"Recipient Estimate" in draft_response.content
    assert b"Snapshot and queue" in draft_response.content
    assert b"Edit draft" in draft_response.content

    campaign.status = "queued"
    campaign.save()
    queued_response = client.get(reverse("mailing:operator_campaign_detail", args=[campaign.id]))
    assert b"Recipient Estimate" not in queued_response.content
    assert b"Snapshot and queue" not in queued_response.content
    assert b"Edit draft" not in queued_response.content


def test_queue_action_snapshots_enqueues_idempotently_and_does_not_call_ses(
    client,
    operator,
    audience,
    client_record,
    monkeypatch,
):
    client.force_login(operator)
    campaign = Campaign.objects.create(
        audience=audience,
        client=client_record,
        subject="Queue me",
        html_body="<p>Hello</p>",
        text_body="Hello",
    )
    create_subscribed_contact("eligible@example.com", audience, client_record)
    create_subscribed_contact("skipped@example.com", audience, client_record, verified=False)
    enqueued = []
    monkeypatch.setattr("mailing.services.campaigns.enqueue_campaign_email", enqueued.append)
    monkeypatch.setattr(
        "mailing.services.campaign_sender.default_ses_client",
        lambda: (_ for _ in ()).throw(AssertionError("SES should not be called by queue action")),
    )

    first = client.post(reverse("mailing:operator_campaign_queue", args=[campaign.id]))
    second = client.post(reverse("mailing:operator_campaign_queue", args=[campaign.id]))

    campaign.refresh_from_db()
    assert first.status_code == 302
    assert second.status_code == 302
    assert campaign.status == "queued"
    assert campaign.recipient_count == 1
    assert campaign.skipped_count == 1
    assert CampaignRecipient.objects.filter(campaign=campaign).count() == 2
    assert len(enqueued) == 1
    assert enqueued[0]["contract"] == "campaign-email"
    assert enqueued[0]["campaign_id"] == campaign.id
    assert enqueued[0]["campaign_recipient_ids"] == list(
        CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.PENDING).values_list(
            "id",
            flat=True,
        )
    )


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
