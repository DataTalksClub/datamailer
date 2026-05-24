from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
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
    ContactExplorerFilters,
    audience_recent_events,
    audience_summary,
    campaign_recipient_queryset,
    campaign_stats,
    contact_detail_context,
    contact_event_timeline,
    contact_explorer_queryset,
    contact_search_queryset,
    metadata_summary,
    parse_contact_explorer_filters,
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


def create_transactional_message(contact, client, *, status=TransactionalMessageStatus.SENT, **kwargs):
    template = EmailTemplate.objects.create(
        client=client,
        key=f"template-{contact.id}-{TransactionalMessage.objects.count()}",
        name="Template",
        subject="Subject",
        is_transactional=True,
    )
    return TransactionalMessage.objects.create(
        client=client,
        contact=contact,
        email=contact.email,
        template=template,
        template_key=template.key,
        status=status,
        subject="Subject",
        **kwargs,
    )


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


def test_contact_explorer_filters_by_scope_tags_status_validation_and_skip_reason(audience, client_record, campaign):
    included = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    excluded = Tag.objects.create(audience=audience, name="Inactive", slug="inactive")
    match = create_subscribed_contact(
        "match@example.com",
        audience,
        client_record,
        email_validation_status=EmailValidationStatus.NO_MX,
    )
    ContactTag.objects.create(contact=match, tag=included)
    CampaignRecipient.objects.create(
        campaign=campaign,
        contact=match,
        email=match.email,
        status=CampaignRecipientStatus.SKIPPED,
        skip_reason="invalid_email",
    )
    no_tag = create_subscribed_contact("no-tag@example.com", audience, client_record)
    excluded_contact = create_subscribed_contact("excluded@example.com", audience, client_record)
    ContactTag.objects.create(contact=excluded_contact, tag=included)
    ContactTag.objects.create(contact=excluded_contact, tag=excluded)

    filters = ContactExplorerFilters(
        audience_id=audience.id,
        client_id=client_record.id,
        include_tags=("newsletter",),
        exclude_tags=("inactive",),
        subscription_status=SubscriptionStatus.SUBSCRIBED,
        verified_state="verified",
        email_validation_status=EmailValidationStatus.NO_MX,
        campaign_status=CampaignRecipientStatus.SKIPPED,
        skip_reason="invalid_email",
    )

    assert list(contact_explorer_queryset(filters)) == [match]
    assert no_tag not in contact_explorer_queryset(filters)
    assert excluded_contact not in contact_explorer_queryset(filters)


def test_contact_explorer_audience_client_filters_match_same_subscription_row(organization):
    audience_one = Audience.objects.create(organization=organization, name="Audience One", slug="audience-one")
    audience_two = Audience.objects.create(organization=organization, name="Audience Two", slug="audience-two")
    client_one = Client.objects.create(organization=organization, name="Client One", slug="client-one")
    client_two = Client.objects.create(organization=organization, name="Client Two", slug="client-two")
    contact = create_contact("split-scope@example.com", verified_at=timezone.now())
    Subscription.objects.create(
        contact=contact,
        audience=audience_one,
        client=client_one,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    Subscription.objects.create(
        contact=contact,
        audience=audience_two,
        client=client_two,
        status=SubscriptionStatus.SUBSCRIBED,
    )

    mismatched = ContactExplorerFilters(audience_id=audience_one.id, client_id=client_two.id)
    matched = ContactExplorerFilters(audience_id=audience_one.id, client_id=client_one.id)

    assert list(contact_explorer_queryset(mismatched)) == []
    assert list(contact_explorer_queryset(matched)) == [contact]


def test_contact_explorer_tag_filters_are_scoped_to_selected_audience(organization):
    audience_one = Audience.objects.create(organization=organization, name="Audience One", slug="audience-one")
    audience_two = Audience.objects.create(organization=organization, name="Audience Two", slug="audience-two")
    client_record = Client.objects.create(organization=organization, name="Client", slug="client")
    tag_one = Tag.objects.create(audience=audience_one, name="Newsletter", slug="newsletter")
    tag_two = Tag.objects.create(audience=audience_two, name="Newsletter", slug="newsletter")
    contact = create_subscribed_contact("cross-tag@example.com", audience_one, client_record)
    ContactTag.objects.create(contact=contact, tag=tag_two)
    matching_contact = create_subscribed_contact("matching-tag@example.com", audience_one, client_record)
    ContactTag.objects.create(contact=matching_contact, tag=tag_one)

    include_filters = ContactExplorerFilters(audience_id=audience_one.id, include_tags=("newsletter",))
    exclude_filters = ContactExplorerFilters(audience_id=audience_one.id, exclude_tags=("newsletter",))

    assert list(contact_explorer_queryset(include_filters)) == [matching_contact]
    assert list(contact_explorer_queryset(exclude_filters)) == [contact]


def test_contact_explorer_engagement_filters_are_deterministic(audience, client_record, campaign):
    now = timezone.now()
    opened_not_clicked = create_subscribed_contact("opened@example.com", audience, client_record)
    never_opened = create_subscribed_contact("never-opened@example.com", audience, client_record)
    clicked = create_subscribed_contact("clicked@example.com", audience, client_record)
    inactive = create_subscribed_contact("inactive@example.com", audience, client_record)
    recently_active = create_subscribed_contact("recent@example.com", audience, client_record)
    empty = create_subscribed_contact("empty@example.com", audience, client_record)
    CampaignRecipient.objects.create(
        campaign=campaign,
        contact=opened_not_clicked,
        email=opened_not_clicked.email,
        status=CampaignRecipientStatus.SENT,
        sent_at=now - timedelta(days=20),
        first_opened_at=now - timedelta(days=10),
    )
    CampaignRecipient.objects.create(
        campaign=campaign,
        contact=never_opened,
        email=never_opened.email,
        status=CampaignRecipientStatus.SENT,
        sent_at=now - timedelta(days=20),
    )
    CampaignRecipient.objects.create(
        campaign=campaign,
        contact=clicked,
        email=clicked.email,
        status=CampaignRecipientStatus.SENT,
        sent_at=now - timedelta(days=20),
        first_opened_at=now - timedelta(days=10),
        first_clicked_at=now - timedelta(days=9),
    )
    create_transactional_message(
        inactive,
        client_record,
        sent_at=now - timedelta(days=40),
    )
    create_transactional_message(
        recently_active,
        client_record,
        sent_at=now - timedelta(days=40),
        first_clicked_at=now - timedelta(days=1),
    )

    scoped = {"audience_id": audience.id, "client_id": client_record.id}

    assert list(contact_explorer_queryset(ContactExplorerFilters(engagement="opened_not_clicked", **scoped))) == [
        opened_not_clicked
    ]
    assert list(contact_explorer_queryset(ContactExplorerFilters(engagement="never_opened", **scoped))) == [
        inactive,
        never_opened,
        recently_active,
    ]
    assert empty not in contact_explorer_queryset(
        ContactExplorerFilters(engagement="inactive_since", inactive_since=(now - timedelta(days=7)).date(), **scoped)
    )
    assert inactive in contact_explorer_queryset(
        ContactExplorerFilters(engagement="inactive_since", inactive_since=(now - timedelta(days=7)).date(), **scoped)
    )
    assert recently_active not in contact_explorer_queryset(
        ContactExplorerFilters(engagement="inactive_since", inactive_since=(now - timedelta(days=7)).date(), **scoped)
    )


def test_parse_contact_explorer_filters_rejects_unknown_values(audience):
    class Params(dict):
        def getlist(self, key):
            value = self.get(key, [])
            return value if isinstance(value, list) else [value]

    filters = parse_contact_explorer_filters(
        Params(
            {
                "audience": str(audience.id),
                "verified": "bad",
                "email_validation_status": "valid",
                "campaign_status": "missing",
                "include_tags": ["newsletter", ""],
                "engagement": "inactive_since",
                "inactive_since": "2026-05-01",
            }
        )
    )

    assert filters.audience_id == audience.id
    assert filters.verified_state == ""
    assert filters.email_validation_status == EmailValidationStatus.VALID
    assert filters.campaign_status == ""
    assert filters.include_tags == ("newsletter",)
    assert filters.engagement == "inactive_since"


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


def test_operator_contact_explorer_renders_filters_and_pagination_querystring(client, operator, audience, client_record):
    client.force_login(operator)
    for index in range(30):
        create_subscribed_contact(f"person-{index:02d}@example.com", audience, client_record)

    response = client.get(
        reverse("mailing:operator_contact_search"),
        {"audience": audience.id, "subscription_status": SubscriptionStatus.SUBSCRIBED},
    )

    assert response.status_code == 200
    assert b"Contact Explorer" in response.content
    assert b"person-00@example.com" in response.content
    assert b"person-29@example.com" not in response.content
    assert b"Page 1 of 2" in response.content
    assert f"audience={audience.id}&amp;subscription_status=subscribed&amp;page=2".encode() in response.content


def test_operator_contact_explorer_empty_state(client, operator):
    client.force_login(operator)

    response = client.get(reverse("mailing:operator_contact_search"), {"q": "missing@example.com"})

    assert response.status_code == 200
    assert b"No contacts match these filters" in response.content


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


def test_contact_detail_eligibility_explains_marketing_and_transactional_blocks(
    client,
    operator,
    audience,
    client_record,
):
    client.force_login(operator)
    contact = create_contact(
        "blocked@example.com",
        verified_at=None,
        email_validation_status=EmailValidationStatus.MANUALLY_INVALID,
        hard_bounced_at=timezone.now(),
    )
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=client_record,
        status=SubscriptionStatus.UNSUBSCRIBED,
    )

    detail = contact_detail_context(contact)
    response = client.get(reverse("mailing:operator_contact_detail", args=[contact.id]))

    assert detail.eligibility[0].can_send_marketing is False
    assert "unverified" in detail.eligibility[0].marketing_reasons
    assert "invalid email validation: Manually invalid" in detail.eligibility[0].marketing_reasons
    assert "client unsubscribe" in detail.eligibility[0].marketing_reasons
    assert detail.eligibility[0].can_send_transactional is False
    assert "hard bounce" in detail.eligibility[0].transactional_reasons
    assert response.status_code == 200
    assert b"Send Eligibility" in response.content
    assert b"invalid email validation: Manually invalid" in response.content
    assert b"client unsubscribe" in response.content


def test_operator_campaign_list_requires_staff(client):
    response = client.get(reverse("mailing:operator_campaign_list"))

    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


def test_operator_audience_views_require_staff(client, audience):
    list_response = client.get(reverse("mailing:operator_audience_list"))
    detail_response = client.get(reverse("mailing:operator_audience_detail", args=[audience.id]))

    assert list_response.status_code == 302
    assert detail_response.status_code == 302
    assert "/admin/login/" in list_response["Location"]
    assert "/admin/login/" in detail_response["Location"]


def test_operator_campaign_list_renders_recent_campaigns(client, operator, campaign):
    client.force_login(operator)

    response = client.get(reverse("mailing:operator_campaign_list"))

    assert response.status_code == 200
    assert b"Weekly update" in response.content
    assert b"DTC Courses" in response.content
    assert b"DataTalksClub" in response.content
    assert b"1 / 3" in response.content
    assert b"Create campaign" in response.content


def test_audience_list_and_detail_render_summaries_members_history_and_events(
    client,
    operator,
    audience,
    client_record,
    campaign,
):
    client.force_login(operator)
    valid = create_subscribed_contact(
        "valid@example.com",
        audience,
        client_record,
        email_validation_status=EmailValidationStatus.VALID,
    )
    invalid = create_subscribed_contact(
        "invalid@example.com",
        audience,
        client_record,
        email_validation_status=EmailValidationStatus.NO_MX,
        hard_bounced_at=timezone.now(),
    )
    tag = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    ContactTag.objects.create(contact=valid, tag=tag)
    CampaignRecipient.objects.create(
        campaign=campaign,
        contact=valid,
        email=valid.email,
        status=CampaignRecipientStatus.SENT,
        sent_at=timezone.now(),
        first_opened_at=timezone.now(),
    )
    CampaignRecipient.objects.create(
        campaign=campaign,
        contact=invalid,
        email=invalid.email,
        status=CampaignRecipientStatus.SKIPPED,
        skip_reason=CampaignRecipientSkipReason.INVALID_EMAIL,
    )
    EmailEvent.objects.create(
        contact=valid,
        audience=audience,
        client=client_record,
        campaign=campaign,
        event_type=EmailEventType.OPEN,
        metadata={"reason": "tracking"},
    )

    summary = {stat.key: stat.value for stat in audience_summary(audience)}
    list_response = client.get(reverse("mailing:operator_audience_list"))
    detail_response = client.get(
        reverse("mailing:operator_audience_detail", args=[audience.id]),
        {"email_validation_status": EmailValidationStatus.NO_MX},
    )

    assert summary["members"] == 2
    assert summary["subscribed"] == 2
    assert summary["hard_bounced"] == 1
    assert list_response.status_code == 200
    assert b"DataTalksClub" in list_response.content
    assert detail_response.status_code == 200
    assert b"Breakdowns" in detail_response.content
    assert b"No MX" in detail_response.content
    assert b"invalid@example.com" in detail_response.content
    assert b"Campaign History" in detail_response.content
    assert b"Recent Events" in detail_response.content
    assert b"reason: tracking" in detail_response.content


def test_audience_recent_events_filters_by_type(audience, client_record):
    contact = create_contact("person@example.com")
    open_event = EmailEvent.objects.create(
        contact=contact,
        audience=audience,
        client=client_record,
        event_type=EmailEventType.OPEN,
    )
    EmailEvent.objects.create(
        contact=contact,
        audience=audience,
        client=client_record,
        event_type=EmailEventType.CLICK,
    )

    assert list(audience_recent_events(audience, EmailEventType.OPEN)) == [open_event]


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
