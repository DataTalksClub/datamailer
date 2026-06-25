from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.urls import reverse
from django.utils import timezone

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    Client,
    ClientApiKey,
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
    active_contact_filters,
    audience_recent_events,
    audience_summary,
    campaign_recipient_queryset,
    campaign_send_progress,
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


def select_active_client(django_client, client_record):
    session = django_client.session
    session[ACTIVE_CLIENT_SESSION_KEY] = client_record.id
    session.save()


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


def test_base_template_loads_datamailer_static_css(client, operator):
    client.force_login(operator)

    response = client.get(reverse("mailing:dashboard"))

    assert response.status_code == 200
    assert b'href="/static/mailing/css/app.css"' in response.content
    assert b"<style>" not in response.content
    assert b'aria-current="page">Datamailer' in response.content
    assert b'data-theme-toggle aria-label="Toggle dark mode"' in response.content
    assert b"datamailer.theme" in response.content
    assert finders.find("mailing/css/app.css") is not None


def test_dashboard_renders_operational_summary_links_and_seeded_style_data(
    client,
    operator,
    audience,
    client_record,
    campaign,
):
    client.force_login(operator)
    bounced = create_contact("bounced@example.com", hard_bounced_at=timezone.now())
    EmailEvent.objects.create(
        contact=bounced,
        client=client_record,
        audience=audience,
        campaign=campaign,
        event_type=EmailEventType.BOUNCE,
        metadata={"bounce_type": "Permanent"},
    )
    ClientApiKey.objects.create(
        client=client_record,
        name="Transactional API",
        key_hash="hashed",
        public_id="dashboarddemo",
    )
    EmailTemplate.objects.create(
        client=client_record,
        key="welcome",
        name="Welcome",
        subject="Welcome",
        is_transactional=True,
        is_active=True,
    )

    response = client.get(reverse("mailing:dashboard"))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Operational Summary" in html
    assert "Recent Campaign Activity" in html
    assert "Deliverability Attention" in html
    assert "Quick Links" in html
    assert "Weekly update" in html
    assert f'href="{reverse("mailing:campaign_detail", args=[campaign.id])}"' in html
    assert "Bounce" in html
    assert "bounce_type: Permanent" in html
    assert 'href="/contacts/bounced@example.com/"' in html
    assert "DTC Courses" in html
    assert f'href="{reverse("mailing:audience_list")}"' in html
    assert f'href="{reverse("mailing:contact_search")}"' in html
    assert "/operator/" not in html


def test_dashboard_empty_states_are_actionable(client, operator):
    client.force_login(operator)

    response = client.get(reverse("mailing:dashboard"))

    assert response.status_code == 200
    assert b"No clients configured" in response.content
    assert f'href="{reverse("mailing:client_create")}"'.encode() in response.content


def test_client_form_pages_remain_staff_only(client, organization, client_record):
    create_url = reverse("mailing:client_create")
    edit_url = reverse("mailing:client_edit", args=[client_record.id])

    assert client.get(create_url).status_code == 302
    assert client.get(edit_url).status_code == 302

    user = get_user_model().objects.create_user("viewer", "viewer@example.com", "password")
    client.force_login(user)

    assert client.get(create_url).status_code == 302
    assert client.get(edit_url).status_code == 302


def test_client_create_form_renders_redesigned_sections_and_redirects_to_detail(client, operator, organization):
    client.force_login(operator)

    response = client.get(reverse("mailing:client_create"))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Create client" in html
    assert "Organization scope" in html
    assert "Integration identity" in html
    assert "API access state" in html
    assert "Client slug" in html
    assert "It is not a secret or API key." in html
    assert "Inactive clients cannot use their API keys for authenticated API activity or sending." in html
    assert f'href="{reverse("mailing:client_list")}"' in html
    assert "Create named API key" not in html

    response = client.post(
        reverse("mailing:client_create"),
        {
            "organization": organization.id,
            "name": "Course Platform",
            "slug": "course-platform",
            "is_active": "on",
        },
    )

    created = Client.objects.get(slug="course-platform")
    assert response.status_code == 302
    assert response.headers["Location"] == reverse("mailing:client_detail", args=[created.id])


def test_client_edit_form_renders_existing_values_and_detail_cancel(client, operator, client_record):
    client.force_login(operator)

    response = client.get(reverse("mailing:client_edit", args=[client_record.id]))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Edit client" in html
    assert f'href="{reverse("mailing:client_detail", args=[client_record.id])}"' in html
    assert f'value="{client_record.name}"' in html
    assert f'value="{client_record.slug}"' in html
    assert "checked" in html
    assert "Save client" in html


def test_client_form_validation_errors_keep_page_structure(client, operator, organization, client_record):
    client.force_login(operator)

    response = client.post(
        reverse("mailing:client_create"),
        {
            "organization": organization.id,
            "name": "Duplicate",
            "slug": client_record.slug,
            "is_active": "on",
        },
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert "Organization scope" in html
    assert "Integration identity" in html
    assert "API access state" in html
    assert "Client slug must be unique within this organization." in html
    assert 'class="field-errors"' in html


def test_rate_hides_unavailable_denominators():
    assert rate(1, 4) == "25.0%"
    assert rate(1, 0) == ""


def test_campaign_stats_include_derived_rates_and_failed_count(campaign):
    create_recipient(campaign, "failed@example.com", status=CampaignRecipientStatus.FAILED)

    stats = {stat.key: stat for stat in campaign_stats(campaign)}

    assert stats["queued"].value == 0
    assert stats["processed"].value == 1
    assert stats["sent"].value == 3
    assert stats["sent"].rate == "75.0%"
    assert stats["delivered"].rate == "66.7%"
    assert stats["failures"].value == 1
    assert stats["failures"].rate == "25.0%"


def test_campaign_send_progress_reports_queue_counts_and_speed(campaign):
    started_at = timezone.now() - timedelta(seconds=120)
    ended_at = started_at + timedelta(seconds=120)
    create_recipient(campaign, "first@example.com", sent_at=started_at)
    create_recipient(campaign, "second@example.com", sent_at=ended_at)
    create_recipient(campaign, "queued@example.com", status=CampaignRecipientStatus.PENDING)
    create_recipient(campaign, "failed@example.com", status=CampaignRecipientStatus.FAILED)

    progress = campaign_send_progress(campaign)

    assert progress.queued_count == 1
    assert progress.processed_count == 3
    assert progress.sent_count == 2
    assert progress.started_at == started_at
    assert progress.ended_at == ended_at
    assert progress.duration_seconds == 120
    assert progress.duration_label == "2m"
    assert progress.per_second == "0.02/sec"
    assert progress.per_minute == "1.0/min"


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


def test_active_contact_filters_summarize_applied_filter_state(audience, client_record):
    filters = ContactExplorerFilters(
        query="person@example.com",
        audience_id=audience.id,
        client_id=client_record.id,
        include_tags=("newsletter",),
        exclude_tags=("inactive",),
        subscription_status=SubscriptionStatus.SUBSCRIBED,
        verified_state="verified",
        email_validation_status=EmailValidationStatus.VALID,
        suppression_state="hard_bounced",
        engagement="inactive_since",
        inactive_since=timezone.datetime(2026, 5, 1).date(),
    )

    chips = active_contact_filters(filters)
    labels = [(chip.label, chip.value) for chip in chips]

    assert ("Email", "person@example.com") in labels
    assert ("Audience", "DataTalksClub") in labels
    assert ("Client", "DTC Courses") in labels
    assert ("Subscription", "Subscribed") in labels
    assert ("Verification", "Verified") in labels
    assert ("Validation", "Valid") in labels
    assert ("Suppression", "Hard bounced") in labels
    assert ("Engagement", "Inactive since 2026-05-01") in labels
    assert ("Includes tag", "newsletter") in labels
    assert ("Excludes tag", "inactive") in labels


def test_operator_contact_views_show_email_validation_status(client, operator, audience, client_record):
    client.force_login(operator)
    contact = create_contact(
        "Person@Example.COM",
        email_validation_status=EmailValidationStatus.MANUALLY_INVALID,
        email_validation_reason="staff marked bad",
        email_validated_at=timezone.now(),
    )
    Subscription.objects.create(contact=contact, audience=audience, client=client_record)

    search_response = client.get(reverse("mailing:contact_search"), {"q": "person@example.com"})
    detail_response = client.get(reverse("mailing:contact_detail", args=[contact.normalized_email]))

    assert search_response.status_code == 200
    assert b"Manually invalid" in search_response.content
    assert b"staff marked bad" in search_response.content
    assert detail_response.status_code == 200
    assert b"Validation" in detail_response.content
    assert b"Manually invalid" in detail_response.content
    assert b"staff marked bad" in detail_response.content


def test_contact_detail_uses_normalized_email_url_and_mixed_case_lookup(client, operator, client_record):
    client.force_login(operator)
    contact = create_contact(" Person@Example.COM ")

    canonical_url = reverse("mailing:contact_detail", args=[contact.normalized_email])
    response = client.get("/contacts/PERSON@EXAMPLE.COM/")

    assert canonical_url == "/contacts/person@example.com/"
    assert response.status_code == 200
    assert b"Person@Example.COM" in response.content
    assert f'action="{reverse("mailing:contact_state_update", args=[contact.normalized_email])}"'.encode() in response.content
    assert f'action="{reverse("mailing:contact_subscription_update", args=[contact.normalized_email])}"'.encode() in response.content
    assert f'action="{reverse("mailing:contact_tag_add", args=[contact.normalized_email])}"'.encode() in response.content
    assert f'action="{reverse("mailing:contact_tag_remove", args=[contact.normalized_email])}"'.encode() in response.content
    assert f"/contacts/{contact.id}/".encode() not in response.content


def test_contact_mutation_routes_redirect_to_normalized_email(client, operator, audience, client_record):
    client.force_login(operator)
    contact = create_contact("Person@Example.COM")
    tag = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    membership = ContactTag.objects.create(contact=contact, tag=tag)

    expected_location = reverse("mailing:contact_detail", args=[contact.normalized_email])
    responses = [
        client.post(
            reverse("mailing:contact_state_update", args=["PERSON@EXAMPLE.COM"]),
            {
                "verified_state": "verified",
                "email_validation_status": EmailValidationStatus.VALID,
                "email_validation_reason": "",
                "global_unsubscribed": "",
                "hard_bounced": "",
                "complained": "",
            },
        ),
        client.post(
            reverse("mailing:contact_subscription_update", args=["PERSON@EXAMPLE.COM"]),
            {
                "audience": audience.id,
                "client": client_record.id,
                "status": SubscriptionStatus.SUBSCRIBED,
                "verified": "on",
                "unsubscribe_reason": "",
            },
        ),
        client.post(
            reverse("mailing:contact_tag_add", args=["PERSON@EXAMPLE.COM"]),
            {"audience": audience.id, "tag": tag.id, "new_tag_name": "", "new_tag_slug": ""},
        ),
        client.post(
            reverse("mailing:contact_tag_remove", args=["PERSON@EXAMPLE.COM"]),
            {"membership": membership.id},
        ),
    ]

    assert [response.status_code for response in responses] == [302, 302, 302, 302]
    assert [response["Location"] for response in responses] == [expected_location] * 4


def test_contact_email_url_unknown_and_numeric_ids_return_404(client, operator, client_record):
    client.force_login(operator)
    contact = create_contact("person@example.com")

    assert client.get("/contacts/missing@example.com/").status_code == 404
    assert client.get(f"/contacts/{contact.id}/").status_code == 404
    assert client.post(f"/contacts/{contact.id}/state/").status_code == 404


def test_operator_contact_explorer_renders_filters_and_pagination_querystring(client, operator, audience, client_record):
    client.force_login(operator)
    for index in range(30):
        create_subscribed_contact(f"person-{index:02d}@example.com", audience, client_record)

    response = client.get(
        reverse("mailing:contact_search"),
        {"audience": audience.id, "subscription_status": SubscriptionStatus.SUBSCRIBED},
    )

    assert response.status_code == 200
    assert b"Contact Explorer" in response.content
    assert b"person-00@example.com" in response.content
    assert b"person-29@example.com" not in response.content
    assert b"Page 1 of 2" in response.content
    assert f"audience={audience.id}&amp;subscription_status=subscribed&amp;page=2".encode() in response.content


def test_operator_contact_explorer_renders_redesigned_filter_groups_and_result_hierarchy(
    client,
    operator,
    audience,
    client_record,
    campaign,
):
    client.force_login(operator)
    now = timezone.now()
    tag = Tag.objects.create(
        audience=audience,
        name="Very Long Newsletter Segment For Returning Learners",
        slug="very-long-newsletter-segment-for-returning-learners",
    )
    reachable = create_subscribed_contact(
        "Reachable.Person.With.Long.Address@example.com",
        audience,
        client_record,
        email_validation_status=EmailValidationStatus.VALID,
    )
    ContactTag.objects.create(contact=reachable, tag=tag)
    CampaignRecipient.objects.create(
        campaign=campaign,
        contact=reachable,
        email=reachable.email,
        status=CampaignRecipientStatus.SENT,
        sent_at=now - timedelta(days=3),
        first_opened_at=now - timedelta(days=2),
    )
    create_subscribed_contact(
        "blocked@example.com",
        audience,
        client_record,
        verified=False,
        status=SubscriptionStatus.UNSUBSCRIBED,
        email_validation_status=EmailValidationStatus.RISKY,
        hard_bounced_at=now,
    )

    response = client.get(
        reverse("mailing:contact_search"),
        {
            "audience": audience.id,
            "subscription_status": SubscriptionStatus.SUBSCRIBED,
            "verified": "verified",
            "email_validation_status": EmailValidationStatus.VALID,
            "engagement": "not_clicked",
            "include_tags": tag.slug,
        },
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert "Filters" in html
    assert "State" in html
    assert "Activity" in html
    assert "Tags" in html
    assert "Applied filters" in html
    assert "Audience: DataTalksClub" in html
    assert "Subscription: Subscribed" in html
    assert "Validation: Valid" in html
    assert "Includes tag: very-long-newsletter-segment-for-returning-learners" in html
    assert 'class="table-wrap contact-explorer-table"' in html
    assert "Last activity" in html
    assert "Opened" in html
    assert "Clicked" in html
    assert 'class="data-truncate" href="/contacts/reachable.person.with.long.address@example.com/"' in html
    assert "Subscribed" in html
    assert "Verified" in html
    assert "Valid" in html
    assert "very-long-newsletter-segment-for-returning-learners" in html
    assert "blocked@example.com" not in html
    assert "/operator/" not in html
    assert f"/contacts/{reachable.id}/" not in html


def test_operator_contact_explorer_badges_suppressed_unverified_and_no_activity_states(
    client,
    operator,
    audience,
    client_record,
):
    client.force_login(operator)
    contact = create_subscribed_contact(
        "suppressed@example.com",
        audience,
        client_record,
        verified=False,
        status=SubscriptionStatus.UNSUBSCRIBED,
        email_validation_status=EmailValidationStatus.MANUALLY_INVALID,
        email_validation_reason="manual block",
        hard_bounced_at=timezone.now(),
    )

    response = client.get(reverse("mailing:contact_search"), {"q": contact.normalized_email})
    html = response.content.decode()

    assert response.status_code == 200
    assert 'href="/contacts/suppressed@example.com/"' in html
    assert "Hard bounced" in html
    assert "Manually invalid" in html
    assert "Unverified" in html
    assert "manual block" in html
    assert "Sent never" in html
    assert "Opened never / Clicked never" in html
    assert "datatalks-club/dtc-courses: unsubscribed" in html


def test_operator_contact_explorer_empty_state(client, operator, client_record):
    client.force_login(operator)

    response = client.get(reverse("mailing:contact_search"), {"q": "missing@example.com"})

    assert response.status_code == 200
    assert b"No contacts match these filters" in response.content
    assert b"Change the email, audience, state, tag, or engagement filters" in response.content


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
    response = client.get(reverse("mailing:contact_detail", args=[contact.normalized_email]))

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


def test_contact_detail_renders_summary_before_management_and_debug_sections(
    client,
    operator,
    audience,
    client_record,
    campaign,
):
    client.force_login(operator)
    contact = create_subscribed_contact(
        "sendable@example.com",
        audience,
        client_record,
        email_validation_status=EmailValidationStatus.VALID,
        email_validated_at=timezone.now(),
    )
    tag = Tag.objects.create(audience=audience, name="Long Newsletter Audience Tag", slug="long-newsletter")
    ContactTag.objects.create(contact=contact, tag=tag)
    recipient = CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        sent_at=timezone.now(),
        delivered_at=timezone.now(),
        first_opened_at=timezone.now(),
        first_clicked_at=timezone.now(),
    )
    create_transactional_message(contact, client_record, sent_at=timezone.now())
    EmailEvent.objects.create(
        contact=contact,
        campaign=campaign,
        campaign_recipient=recipient,
        client=client_record,
        audience=audience,
        event_type=EmailEventType.CLICK,
        url="https://example.com/click",
        metadata={"scope": "campaign"},
    )

    response = client.get(reverse("mailing:contact_detail", args=[contact.normalized_email]))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Can send marketing" in html
    assert "Can send transactional" in html
    assert "Last sent" in html
    assert "Long Newsletter Audience Tag" in html
    assert "Recent Activity" in html
    assert "Campaign: Weekly update" in html
    assert f"/campaigns/{campaign.id}/#recipient-{recipient.id}" in html
    assert html.index("Send Eligibility") < html.index("Membership and Tags") < html.index("Recent Activity")
    assert html.index("Recent Activity") < html.index("Manage contact state, subscriptions, and tags")
    assert html.index("Full event timeline and audit details") > html.index("Recent Activity")


def test_contact_detail_summary_shows_blocked_reasons_and_secondary_raw_details(
    client,
    operator,
    audience,
    client_record,
):
    client.force_login(operator)
    contact = create_contact(
        "blocked@example.com",
        email_validation_status=EmailValidationStatus.RISKY,
        email_validation_reason="provider risk score",
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
    EmailEvent.objects.create(
        contact=contact,
        client=client_record,
        audience=audience,
        event_type=EmailEventType.BOUNCE,
        metadata={"bounce_type": "Permanent"},
    )

    response = client.get(reverse("mailing:contact_detail", args=[contact.normalized_email]))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Risky" in html
    assert "Globally unsubscribed" in html
    assert "Cannot send marketing" in html
    assert "Cannot send transactional" in html
    assert "global unsubscribe" in html
    assert "client unsubscribe" in html
    assert "hard bounce" in html
    assert "complaint" in html
    assert "bounce_type: Permanent" in html
    assert html.index("bounce_type: Permanent") > html.index("Full event timeline and audit details")


def test_contact_detail_no_membership_explains_not_subscribed_and_no_activity(client, operator, client_record):
    client.force_login(operator)
    contact = create_contact("quiet@example.com", email_validation_status=EmailValidationStatus.UNKNOWN)

    response = client.get(reverse("mailing:contact_detail", args=[contact.normalized_email]))
    html = response.content.decode()

    assert response.status_code == 200
    assert "No subscriptions" in html
    assert "Unknown" in html
    assert "Cannot send marketing" in html
    assert "not subscribed" in html
    assert "Can send transactional" in html
    assert "No data" in html
    assert "No recent campaign, transactional, or contact events found." in html


def test_campaign_list_requires_staff(client):
    response = client.get(reverse("mailing:campaign_list"))

    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


def test_operator_audience_views_require_staff(client, audience):
    list_response = client.get(reverse("mailing:audience_list"))
    detail_response = client.get(reverse("mailing:audience_detail", args=[audience.id]))

    assert list_response.status_code == 302
    assert detail_response.status_code == 302
    assert "/admin/login/" in list_response["Location"]
    assert "/admin/login/" in detail_response["Location"]


def test_campaign_list_renders_recent_campaigns(client, operator, campaign):
    client.force_login(operator)
    started_at = timezone.now() - timedelta(seconds=60)
    create_recipient(campaign, "list-sent@example.com", sent_at=started_at)
    create_recipient(campaign, "list-sent-2@example.com", sent_at=started_at + timedelta(seconds=60))
    create_recipient(campaign, "list-queued@example.com", status=CampaignRecipientStatus.PENDING)

    response = client.get(reverse("mailing:campaign_list"))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Weekly update" in html
    assert "DTC Courses" in html
    assert "DataTalksClub" in html
    assert "Send time" in html
    assert "Queue" in html
    assert "Speed" in html
    assert "1 queued / 2 processed" in html
    assert "0.03/sec" in html
    assert "2.0/min" in html
    assert "3 sent / 2 delivered" in html
    assert "1 opens / 1 clicks" in html
    assert "1 bounces / 1 complaints" in html
    assert '<span class="badge success">Sent</span>' in html
    assert "Create campaign" in html


def test_campaign_list_empty_state_is_actionable(client, operator, client_record):
    client.force_login(operator)

    response = client.get(reverse("mailing:campaign_list"))

    assert response.status_code == 200
    assert b"No campaigns yet" in response.content
    assert b"Create campaign" in response.content
    assert f'href="{reverse("mailing:campaign_create")}"'.encode() in response.content


def test_audience_list_empty_state_is_actionable(client, operator, client_record):
    client.force_login(operator)

    response = client.get(reverse("mailing:audience_list"))

    assert response.status_code == 200
    assert b"No audiences yet" in response.content
    assert b"Create audience" in response.content
    assert f'href="{reverse("mailing:audience_create")}"'.encode() in response.content


def test_audience_create_and_edit_forms_use_operational_layout(client, operator, audience, client_record):
    client.force_login(operator)

    create_response = client.get(reverse("mailing:audience_create"))
    edit_response = client.get(reverse("mailing:audience_edit", args=[audience.id]))

    assert create_response.status_code == 200
    assert edit_response.status_code == 200
    for html in (create_response.content.decode(), edit_response.content.decode()):
        assert '<form class="form-page" method="post" novalidate>' in html
        assert "Organization scope" in html
        assert "Audience identity" in html
        assert "The selected organization scopes this audience and its slug." in html
        assert "Lowercase identifier; must be unique within the selected organization." in html
        assert "Audience name" in html
        assert "Audience slug" in html
        assert f'href="{reverse("mailing:audience_list")}"' in html
        assert 'class="action-row"' in html
        assert 'class="button secondary"' in html
    assert "Create audience" in create_response.content.decode()
    assert "Save audience" in edit_response.content.decode()


def test_tag_create_and_edit_forms_show_parent_scope_and_actions(client, operator, audience, client_record):
    client.force_login(operator)
    tag = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")

    create_response = client.get(reverse("mailing:tag_create", args=[audience.id]))
    edit_response = client.get(reverse("mailing:tag_edit", args=[tag.id]))

    assert create_response.status_code == 200
    assert edit_response.status_code == 200
    for html in (create_response.content.decode(), edit_response.content.decode()):
        assert '<form class="form-page" method="post" novalidate>' in html
        assert "Parent audience" in html
        assert "This tag belongs to exactly one audience." in html
        assert "Tag identity" in html
        assert "Lowercase identifier; must be unique within this audience." in html
        assert "Tag name" in html
        assert "Tag slug" in html
        assert audience.name in html
        assert audience.slug in html
        assert audience.organization.name in html
        assert f'href="{reverse("mailing:audience_detail", args=[audience.id])}"' in html
        assert 'class="action-row"' in html
        assert 'class="button secondary"' in html
    assert "Create tag" in create_response.content.decode()
    assert "Save tag" in edit_response.content.decode()


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
    inactive = create_subscribed_contact("inactive@example.com", audience, client_record)
    tag = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    ContactTag.objects.create(contact=valid, tag=tag)
    ContactTag.objects.create(contact=inactive, tag=tag)
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
    CampaignRecipient.objects.create(
        campaign=campaign,
        contact=inactive,
        email=inactive.email,
        status=CampaignRecipientStatus.SENT,
        sent_at=timezone.now(),
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
    list_response = client.get(reverse("mailing:audience_list"))
    detail_response = client.get(
        reverse("mailing:audience_detail", args=[audience.id]),
        {"email_validation_status": EmailValidationStatus.NO_MX},
    )

    assert summary["members"] == 3
    assert summary["subscribed"] == 3
    assert summary["inactive"] == 1
    assert summary["hard_bounced"] == 1
    assert list_response.status_code == 200
    list_html = list_response.content.decode()
    assert "Audience Health" in list_html
    assert "3 members" in list_html
    assert "3 subscribed" in list_html
    assert "1 inactive" in list_html
    assert "1 suppressed" in list_html
    assert "Weekly update" in list_html
    assert f'href="{reverse("mailing:audience_detail", args=[audience.id])}"' in list_html
    assert detail_response.status_code == 200
    detail_html = detail_response.content.decode()
    assert "Segmentation" in detail_html
    assert "Membership" in detail_html
    assert "Inactive since" in detail_html
    assert 'name="include_tags" value="newsletter"' in detail_html
    assert 'class="table-wrap audience-member-table"' in detail_html
    assert "No MX" in detail_html
    assert "Hard bounced" in detail_html
    assert "invalid@example.com" in detail_html
    assert 'href="/contacts/invalid@example.com/"' in detail_html
    assert "/operator/" not in detail_html
    assert "Campaign History" in detail_html
    assert "Recent Events" in detail_html
    assert "reason: tracking" in detail_html


def test_audience_detail_membership_summaries_are_scoped_to_current_audience(
    client,
    operator,
    organization,
    audience,
    client_record,
    campaign,
):
    client.force_login(operator)
    other_organization = Organization.objects.create(name="AI Shipping Labs", slug="ai-shipping-labs")
    other_audience = Audience.objects.create(
        organization=other_organization,
        name="AI Shipping Labs",
        slug="ai-shipping-labs",
    )
    other_client = Client.objects.create(
        organization=other_organization,
        name="ASL Platform",
        slug="asl-platform",
    )
    select_active_client(client, client_record)
    contact = create_subscribed_contact("multi@example.com", audience, client_record)
    Subscription.objects.create(
        contact=contact,
        audience=other_audience,
        client=other_client,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    current_tag = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    other_tag = Tag.objects.create(audience=other_audience, name="Founder", slug="founder")
    ContactTag.objects.create(contact=contact, tag=current_tag)
    ContactTag.objects.create(contact=contact, tag=other_tag)
    other_campaign = Campaign.objects.create(
        audience=other_audience,
        client=other_client,
        subject="Other audience failure",
        status="sent",
    )
    CampaignRecipient.objects.create(
        campaign=other_campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.FAILED,
    )
    CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        sent_at=timezone.now(),
    )

    response = client.get(reverse("mailing:audience_detail", args=[audience.id]), {"q": "multi@example.com"})

    assert response.status_code == 200
    html = response.content.decode()
    assert "multi@example.com" in html
    assert "datatalks-club/dtc-courses: subscribed" in html
    assert "datatalks-club/newsletter" in html
    assert "ai-shipping-labs/asl-platform" not in html
    assert "ai-shipping-labs/founder" not in html
    assert "Other audience failure" not in html


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
    started_at = timezone.now() - timedelta(seconds=120)
    recipient = create_recipient(
        campaign,
        "person@example.com",
        sent_at=started_at,
        first_opened_at=timezone.now(),
        first_clicked_at=timezone.now(),
        open_count=2,
        click_count=1,
        ses_message_id="ses-123",
        last_error="",
    )
    create_recipient(campaign, "other@example.com", sent_at=started_at + timedelta(seconds=120))
    create_recipient(campaign, "pending@example.com", status=CampaignRecipientStatus.PENDING)
    EmailEvent.objects.create(
        campaign=campaign,
        campaign_recipient=recipient,
        contact=recipient.contact,
        client=campaign.client,
        audience=campaign.audience,
        event_type=EmailEventType.OPEN,
        provider_event_id="evt-123",
        metadata={"ses_message_id": "ses-123"},
    )

    response = client.get(reverse("mailing:campaign_detail", args=[campaign.id]), {"filter": "opened"})
    html = response.content.decode()

    assert response.status_code == 200
    assert "Summary" in html
    assert "Stats" in html
    assert "Send started" in html
    assert "Send ended" in html
    assert "Send duration" in html
    assert "Send speed" in html
    assert "Queued" in html
    assert "Processed" in html
    assert "2m" in html
    assert "0.02/sec" in html
    assert "1.0/min" in html
    assert "Recipients" in html
    assert "Recent History" in html
    assert "Provider data" in html
    assert "campaign-recipient-table" in html
    assert "email-cell" in html
    assert "provider-cell" in html
    assert "data-truncate" in html
    assert '<span class="badge success">Sent</span>' in html
    assert "Unique opens" in html
    assert "33.3%" in html
    assert recipient.email in html
    assert "other@example.com" not in html
    assert "ses-123" in html
    assert "Provider event: evt-123" in html
    assert 'href="/contacts/person@example.com/"' in html
    assert f'id="recipient-{recipient.id}"' in html


def test_campaign_create_form_uses_sectioned_operational_layout(client, operator, audience, client_record):
    client.force_login(operator)
    Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")

    response = client.get(reverse("mailing:campaign_create"))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Audience and sender" in html
    assert "Recipient filters" in html
    assert "Campaign metadata" in html
    assert "Final pasted content" in html
    assert "Sending controls" in html
    assert "Include tags narrow the audience" in html
    assert "Tags must belong to the selected audience" in html
    assert "A tag cannot be both included and excluded" in html
    assert "Paste the final HTML email body prepared outside Datamailer" in html
    assert 'name="html_body"' in html
    assert 'rows="18"' in html
    assert 'name="text_body"' in html
    assert 'rows="12"' in html
    assert 'class="action-row"' in html
    assert f'href="{reverse("mailing:campaign_list")}"' in html


def test_campaign_edit_form_cancel_returns_to_campaign_detail(client, operator, audience, client_record):
    client.force_login(operator)
    campaign = Campaign.objects.create(
        audience=audience,
        client=client_record,
        subject="Draft",
        html_body="<p>Ready</p>",
        text_body="Ready",
    )

    response = client.get(reverse("mailing:campaign_edit", args=[campaign.id]))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Edit campaign" in html
    assert "Save draft" in html
    assert f'href="{reverse("mailing:campaign_detail", args=[campaign.id])}"' in html


def test_operator_can_create_campaign_draft_with_tag_filters(client, operator, audience, client_record):
    client.force_login(operator)
    include_tag = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    exclude_tag = Tag.objects.create(audience=audience, name="Inactive", slug="inactive")

    response = client.post(
        reverse("mailing:campaign_create"),
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
    assert response["Location"] == reverse("mailing:campaign_detail", args=[campaign.id])
    assert campaign.status == "draft"
    assert campaign.preview_text == "Final preview"
    assert campaign.html_body == "<p>Final HTML</p>"
    assert campaign.text_body == "Final text"
    assert campaign.include_tags == ["newsletter"]
    assert campaign.exclude_tags == ["inactive"]


def test_campaign_create_validation_rejects_same_tag_in_include_and_exclude(client, operator, audience, client_record):
    client.force_login(operator)
    tag = Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")

    response = client.post(
        reverse("mailing:campaign_create"),
        {
            "audience": audience.id,
            "client": client_record.id,
            "subject": "Invalid filters",
            "html_body": "<p>Final HTML</p>",
            "text_body": "Final text",
            "include_tags": [tag.id],
            "exclude_tags": [tag.id],
        },
    )

    assert response.status_code == 200
    assert Campaign.objects.count() == 0
    assert b"A tag cannot be both included and excluded" in response.content


def test_campaign_create_validation_rejects_missing_final_bodies(client, operator, audience, client_record):
    client.force_login(operator)

    response = client.post(
        reverse("mailing:campaign_create"),
        {
            "audience": audience.id,
            "subject": "Incomplete",
            "html_body": "",
            "text_body": "",
        },
    )

    assert response.status_code == 200
    assert Campaign.objects.count() == 0
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
        reverse("mailing:campaign_edit", args=[draft.id]),
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
        reverse("mailing:campaign_edit", args=[draft.id]),
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

    draft_response = client.get(reverse("mailing:campaign_detail", args=[campaign.id]))

    assert draft_response.status_code == 200
    assert b"Queue Preview" in draft_response.content
    assert b"Snapshot and queue" in draft_response.content
    assert b"Edit draft" in draft_response.content
    assert b"No campaign email events found yet" in draft_response.content

    campaign.status = "queued"
    campaign.save()
    queued_response = client.get(reverse("mailing:campaign_detail", args=[campaign.id]))
    assert b"Queue Preview" not in queued_response.content
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

    first = client.post(reverse("mailing:campaign_queue", args=[campaign.id]))
    second = client.post(reverse("mailing:campaign_queue", args=[campaign.id]))

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

    response = client.get(reverse("mailing:campaign_detail", args=[campaign.id]))

    assert response.status_code == 200
    assert b"person-00@example.com" in response.content
    assert b"person-54@example.com" not in response.content
    assert b"Page 1 of 2" in response.content


def test_contact_search_and_detail_render_product_context(client, operator, audience, client_record, campaign):
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

    search_response = client.get(reverse("mailing:contact_search"), {"q": "person@example.com"})
    detail_response = client.get(reverse("mailing:contact_detail", args=[contact.normalized_email]))

    assert search_response.status_code == 200
    assert b"Person@Example.COM" in search_response.content
    assert detail_response.status_code == 200
    assert b"Global unsubscribe" in detail_response.content
    assert b"requested" in detail_response.content
    assert b"Newsletter" in detail_response.content
    assert b'href="/contacts/person@example.com/"' in search_response.content
    assert f"/campaigns/{campaign.id}/#recipient-{recipient.id}".encode() in detail_response.content
    assert b"Transactional Messages" in detail_response.content
    assert b"Unsubscribe" in detail_response.content
    assert b"scope: global" in detail_response.content
    assert b"Operator Audit" not in detail_response.content
    assert b"No operator audit entries found" not in detail_response.content


def test_transactional_template_catalog_is_staff_only(client, operator, client_record):
    contact = create_contact("Person@Example.COM")
    template = EmailTemplate.objects.create(
        client=client_record,
        key="email-verification",
        name="Email Verification",
        description="Verify a client account email.",
        subject="Verify {{ product }}",
        text_body="Verify at {{ verification_url }}",
        required_context=[
            {"name": "product", "description": "Product name."},
            {"name": "verification_url", "description": "Client-generated verification URL."},
        ],
        example_context={
            "product": "Datamailer",
            "verification_url": "https://client.example/verify/placeholder",
        },
    )
    TransactionalMessage.objects.create(
        client=client_record,
        contact=contact,
        email=contact.email,
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.SENT,
        subject="Verify account",
    )

    anonymous = client.get(reverse("mailing:template_catalog"))
    assert anonymous.status_code == 302
    assert "/admin/login/" in anonymous["Location"]
    anonymous_detail = client.get(reverse("mailing:template_detail", args=[template.id]))
    assert anonymous_detail.status_code == 302
    assert "/admin/login/" in anonymous_detail["Location"]

    regular_user = get_user_model().objects.create_user("regular", "regular@example.com", "password", is_staff=False)
    client.force_login(regular_user)
    non_staff = client.get(reverse("mailing:template_catalog"))
    assert non_staff.status_code == 302
    assert "/admin/login/" in non_staff["Location"]
    non_staff_detail = client.get(reverse("mailing:template_detail", args=[template.id]))
    assert non_staff_detail.status_code == 302
    assert "/admin/login/" in non_staff_detail["Location"]

    client.force_login(operator)
    list_response = client.get(reverse("mailing:template_catalog"))
    detail_response = client.get(reverse("mailing:template_detail", args=[template.id]))

    assert list_response.status_code == 200
    assert b"email-verification" in list_response.content
    assert b"Active" in list_response.content
    assert b"verification_url" in list_response.content
    assert detail_response.status_code == 200
    assert b"Verify a client account email." in detail_response.content
    assert b"Client-generated verification URL." in detail_response.content
    assert b"https://client.example/verify/placeholder" in detail_response.content
    assert b"Verify at https://client.example/verify/placeholder" in detail_response.content
    assert b'href="/contacts/person@example.com/"' in detail_response.content
    assert b'<span class="badge success">Sent</span>' in detail_response.content


def test_transactional_template_catalog_filters_paginates_and_summarizes_context(client, operator, organization):
    client.force_login(operator)
    selected_client = Client.objects.create(organization=organization, name="Selected Client", slug="selected-client")
    other_client = Client.objects.create(organization=organization, name="Other Client", slug="other-client")
    select_active_client(client, selected_client)
    for index in range(26):
        EmailTemplate.objects.create(
            client=selected_client,
            key=f"selected-template-{index:02d}",
            name=f"Selected Template {index:02d}",
            subject="Subject",
            required_context=[
                {"name": "first_name", "description": "Recipient first name."},
                {"name": "verification_url", "description": "Verification URL."},
                {"name": "product_name", "description": "Product name."},
                {"name": "support_email", "description": "Support address."},
            ],
        )
    EmailTemplate.objects.create(
        client=other_client,
        key="other-template",
        name="Other Template",
        subject="Subject",
    )

    response = client.get(reverse("mailing:template_catalog"))

    assert response.status_code == 200
    html = response.content.decode()
    assert "selected-template-00" in html
    assert "other-template" not in html
    assert "first_name" in html
    assert "verification_url" in html
    assert "+1" in html
    assert "{&#x27;name&#x27;" not in html
    assert "?page=2" in html


def test_transactional_template_pages_show_operational_empty_states(client, operator, client_record):
    client.force_login(operator)
    empty_client = Client.objects.create(
        organization=client_record.organization,
        name="Empty Client",
        slug="empty-client",
    )
    template = EmailTemplate.objects.create(
        client=client_record,
        key="empty-context",
        name="Empty Context",
        subject="",
        text_body="",
        html_body="",
        required_context=[],
        example_context={},
    )

    select_active_client(client, empty_client)
    filtered_empty = client.get(reverse("mailing:template_catalog"))
    select_active_client(client, client_record)
    detail_response = client.get(reverse("mailing:template_detail", args=[template.id]))

    assert filtered_empty.status_code == 200
    assert b"Adjust the client filter" in filtered_empty.content
    assert detail_response.status_code == 200
    assert b"This template can render without caller-supplied context." in detail_response.content
    assert b"Preview rendering will use an empty context." in detail_response.content
    assert b"Add a subject, text body, or HTML body before using this template." in detail_response.content
    assert b"Messages sent with this template will appear here for debugging." in detail_response.content


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

    response = client.get(reverse("mailing:contact_detail", args=[contact.normalized_email]))

    assert response.status_code == 200
    assert b"Page 1 of 2" in response.content
    assert response.content.count(b"<strong>Open</strong>") == 50
