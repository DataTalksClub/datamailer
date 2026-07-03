from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time

from django.db.models import Count, Exists, Max, Min, OuterRef, Q, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_date

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    ClientApiKey,
    Contact,
    ContactTag,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    EmailValidationStatus,
    Subscription,
    SubscriptionStatus,
    Tag,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.contacts import has_invalid_email_validation, normalize_email
from mailing.services.worker_status import WorkerStatus, sandbox_worker_statuses

RECIPIENT_FILTERS = {
    "opened": Q(first_opened_at__isnull=False),
    "clicked": Q(first_clicked_at__isnull=False),
    "not_opened": Q(first_opened_at__isnull=True),
    "bounced": Q(status=CampaignRecipientStatus.BOUNCED),
    "unsubscribed": Q(status=CampaignRecipientStatus.UNSUBSCRIBED),
    "complained": Q(status=CampaignRecipientStatus.COMPLAINED),
    "skipped": Q(status=CampaignRecipientStatus.SKIPPED),
    "failed": Q(status=CampaignRecipientStatus.FAILED),
    "sent": Q(status=CampaignRecipientStatus.SENT),
    "pending": Q(status=CampaignRecipientStatus.PENDING),
}

PROCESSED_RECIPIENT_STATUSES = {
    CampaignRecipientStatus.SENT,
    CampaignRecipientStatus.FAILED,
    CampaignRecipientStatus.BOUNCED,
    CampaignRecipientStatus.COMPLAINED,
    CampaignRecipientStatus.UNSUBSCRIBED,
}

RECIPIENT_FILTER_LABELS = {
    "opened": "Opened",
    "clicked": "Clicked",
    "not_opened": "Not opened",
    "bounced": "Bounced",
    "unsubscribed": "Unsubscribed",
    "complained": "Complained",
    "skipped": "Skipped",
    "failed": "Failed",
    "sent": "Sent",
    "pending": "Pending",
}

ENGAGEMENT_FILTER_LABELS = {
    "not_opened": "Not opened",
    "not_clicked": "Not clicked",
    "opened_not_clicked": "Opened, not clicked",
    "never_opened": "Never opened",
    "never_clicked": "Never clicked",
    "inactive_since": "Inactive since",
}

SUPPRESSION_FILTER_LABELS = {
    "global_unsubscribed": "Global unsubscribed",
    "hard_bounced": "Hard bounced",
    "complained": "Complained",
}

VERIFIED_FILTER_LABELS = {
    "verified": "Verified",
    "unverified": "Unverified",
}


@dataclass(frozen=True)
class Stat:
    key: str
    label: str
    value: int
    rate: str = ""


@dataclass(frozen=True)
class Choice:
    value: str
    label: str


@dataclass(frozen=True)
class ContactExplorerFilters:
    query: str = ""
    audience_id: int | None = None
    client_id: int | None = None
    include_tags: tuple[str, ...] = ()
    exclude_tags: tuple[str, ...] = ()
    subscription_status: str = ""
    verified_state: str = ""
    email_validation_status: str = ""
    suppression_state: str = ""
    campaign_status: str = ""
    skip_reason: str = ""
    engagement: str = ""
    inactive_since: object | None = None

    @property
    def has_filters(self):
        return any(
            (
                self.query,
                self.audience_id,
                self.client_id,
                self.include_tags,
                self.exclude_tags,
                self.subscription_status,
                self.verified_state,
                self.email_validation_status,
                self.suppression_state,
                self.campaign_status,
                self.skip_reason,
                self.engagement,
            )
        )


@dataclass(frozen=True)
class ContactResultRow:
    contact: Contact
    verification_badge: "Badge"
    validation_badge: "Badge"
    subscription_badge: "Badge"
    subscription_summary: str
    tag_summary: str
    last_sent_at: object | None
    last_opened_at: object | None
    last_clicked_at: object | None
    recent_issue: str


@dataclass(frozen=True)
class ActiveFilter:
    label: str
    value: str


@dataclass(frozen=True)
class EligibilityItem:
    scope: str
    can_send_marketing: bool
    marketing_reasons: tuple[str, ...]
    can_send_transactional: bool
    transactional_reasons: tuple[str, ...]


@dataclass(frozen=True)
class Badge:
    label: str
    tone: str = "neutral"


@dataclass(frozen=True)
class CampaignListRow:
    campaign: Campaign
    badge: Badge
    timing_label: str
    timing_value: object | None
    progress: "CampaignSendProgress"


@dataclass(frozen=True)
class CampaignSendProgress:
    queued_count: int
    processed_count: int
    sent_count: int
    started_at: object | None
    ended_at: object | None
    duration_seconds: int | None
    duration_label: str
    per_second: str
    per_minute: str

    @property
    def has_started(self) -> bool:
        return self.started_at is not None


@dataclass(frozen=True)
class AudienceListRow:
    audience: Audience
    member_count: int
    subscribed_count: int
    inactive_count: int
    suppressed_count: int
    campaign_count: int
    recent_campaign: Campaign | None
    recent_event: EmailEvent | None


@dataclass(frozen=True)
class ContactMetric:
    label: str
    value: object | None
    tone: str = "neutral"


@dataclass(frozen=True)
class SendabilitySummary:
    marketing_badge: Badge
    marketing_reasons: tuple[str, ...]
    transactional_badge: Badge
    transactional_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ContactActivity:
    occurred_at: object | None
    badge: Badge
    title: str
    detail: str
    href: str = ""
    metadata: str = ""


@dataclass(frozen=True)
class ContactDetailContext:
    eligibility: list[EligibilityItem]
    subscriptions: QuerySet[Subscription]
    contact_tags: QuerySet[ContactTag]
    verification_badge: Badge
    validation_badge: Badge
    subscription_badge: Badge
    sendability: SendabilitySummary
    metrics: list[ContactMetric]
    recent_activity: list[ContactActivity]


@dataclass(frozen=True)
class DashboardCampaign:
    campaign: Campaign
    badge: Badge


@dataclass(frozen=True)
class DashboardAttentionItem:
    occurred_at: object | None
    badge: Badge
    title: str
    detail: str
    href: str = ""


@dataclass(frozen=True)
class DashboardClient:
    client: Client
    active_api_key_count: int


@dataclass(frozen=True)
class DashboardContext:
    summary_stats: list[Stat]
    recent_campaigns: list[DashboardCampaign]
    attention_items: list[DashboardAttentionItem]
    worker_statuses: list[WorkerStatus]
    integration_stats: list[Stat]
    integration_clients: list[DashboardClient]
    transactional_backlog_count: int


def rate(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return ""
    return f"{(numerator / denominator) * 100:.1f}%"


def choices_from_text_choices(text_choices) -> list[Choice]:
    return [Choice(value=value, label=label) for value, label in text_choices.choices]


def dashboard_context(client: Client | None = None) -> DashboardContext:
    campaign_scope = Campaign.objects.all()
    subscription_scope = Subscription.objects.all()
    template_scope = EmailTemplate.objects.all()
    client_scope = Client.objects.all()
    audience_scope = Audience.objects.all()
    scoped_contacts = Contact.objects.all()
    if client is not None:
        campaign_scope = campaign_scope.filter(client=client)
        subscription_scope = subscription_scope.filter(client=client)
        template_scope = template_scope.filter(client=client)
        client_scope = client_scope.filter(pk=client.pk)
        audience_scope = audience_scope.filter(organization=client.organization)
        scoped_contacts = scoped_contacts.filter(
            Q(subscriptions__client=client)
            | Q(campaign_recipients__campaign__client=client)
            | Q(transactional_messages__client=client)
            | Q(email_events__client=client)
        ).distinct()

    active_campaigns = campaign_scope.filter(
        status__in=[
            CampaignStatus.QUEUED,
            CampaignStatus.SNAPSHOTTING,
            CampaignStatus.SENDING,
        ]
    ).count()
    draft_campaigns = campaign_scope.filter(status=CampaignStatus.DRAFT).count()
    subscribed_contacts = (
        subscription_scope.filter(status=SubscriptionStatus.SUBSCRIBED).values("contact_id").distinct().count()
    )
    suppressed_contacts = suppressed_contact_queryset(client).count()
    hard_bounces = scoped_contacts.filter(hard_bounced_at__isnull=False).count()
    complaints = scoped_contacts.filter(complained_at__isnull=False).count()
    active_clients = client_scope.filter(is_active=True).count()
    active_api_keys = ClientApiKey.objects.filter(client__in=client_scope, revoked_at__isnull=True).count()
    active_templates = template_scope.filter(is_transactional=True, is_active=True).count()

    transactional_backlog_scope = TransactionalMessage.objects.filter(status=TransactionalMessageStatus.QUEUED)
    if client is not None:
        transactional_backlog_scope = transactional_backlog_scope.filter(client=client)
    transactional_backlog_count = transactional_backlog_scope.count()

    recent_campaigns = [
        DashboardCampaign(campaign, campaign_status_badge(campaign.status))
        for campaign in campaign_scope.select_related("client", "audience").order_by("-updated_at", "-id")[:5]
    ]
    integration_clients = [
        DashboardClient(scope_client, scope_client.active_api_key_count)
        for scope_client in client_scope.select_related("organization").order_by("organization__slug", "slug")[:4]
    ]

    return DashboardContext(
        summary_stats=[
            Stat(
                "campaigns",
                "Campaigns",
                campaign_scope.count(),
                f"{active_campaigns} active / {draft_campaigns} drafts",
            ),
            Stat(
                "contacts",
                "Contacts",
                scoped_contacts.count(),
                f"{subscribed_contacts} subscribed / {audience_scope.count()} audiences",
            ),
            Stat(
                "deliverability",
                "Deliverability attention",
                suppressed_contacts,
                f"{hard_bounces} bounces / {complaints} complaints",
            ),
            Stat("api_access", "API access", active_clients, f"{active_api_keys} active keys"),
            Stat("templates", "Transactional templates", active_templates, "active templates"),
        ],
        recent_campaigns=recent_campaigns,
        attention_items=dashboard_attention_items(client),
        worker_statuses=sandbox_worker_statuses(),
        integration_stats=[
            Stat("clients", "Active clients", active_clients, f"{client_scope.count()} total"),
            Stat("api_keys", "Active API keys", active_api_keys),
            Stat("templates", "Active templates", active_templates),
        ],
        integration_clients=integration_clients,
        transactional_backlog_count=transactional_backlog_count,
    )


def suppressed_contact_queryset(client: Client | None = None):
    queryset = Contact.objects.filter(
        Q(global_unsubscribed_at__isnull=False)
        | Q(hard_bounced_at__isnull=False)
        | Q(complained_at__isnull=False)
        | Q(subscriptions__status=SubscriptionStatus.UNSUBSCRIBED)
    ).distinct()
    if client is not None:
        queryset = queryset.filter(
            Q(subscriptions__client=client)
            | Q(campaign_recipients__campaign__client=client)
            | Q(transactional_messages__client=client)
            | Q(email_events__client=client)
        ).distinct()
    return queryset


def dashboard_attention_items(client: Client | None = None) -> list[DashboardAttentionItem]:
    event_queryset = EmailEvent.objects.select_related("contact", "client", "audience", "campaign")
    if client is not None:
        event_queryset = event_queryset.filter(client=client)
    event_items = [
        DashboardAttentionItem(
            event.created_at,
            Badge(event.get_event_type_display(), event_tone(event.event_type)),
            event_context(event),
            metadata_summary(event.metadata) or event.url or "Email event recorded",
            href=f"/contacts/{event.contact.normalized_email}/" if event.contact_id else "",
        )
        for event in event_queryset.filter(
            event_type__in=[
                EmailEventType.BOUNCE,
                EmailEventType.COMPLAINT,
                EmailEventType.FAILED,
                EmailEventType.SKIPPED,
                EmailEventType.UNSUBSCRIBE,
            ]
        ).order_by("-created_at", "-id")[:5]
    ]
    if event_items:
        return event_items

    return [
        DashboardAttentionItem(
            latest_datetime(
                contact.global_unsubscribed_at,
                contact.hard_bounced_at,
                contact.complained_at,
                contact.updated_at,
            ),
            subscription_badge(contact, contact.subscriptions.all()),
            contact.normalized_email,
            "Suppressed contact needs review before future sends.",
            href=f"/contacts/{contact.normalized_email}/",
        )
        for contact in suppressed_contact_queryset(client)
        .prefetch_related("subscriptions")
        .order_by("-updated_at", "normalized_email")[:5]
    ]


def campaign_status_badge(status: str) -> Badge:
    label = CampaignStatus(status).label
    if status == CampaignStatus.SENT:
        return Badge(label, "success")
    if status in {CampaignStatus.QUEUED, CampaignStatus.SNAPSHOTTING, CampaignStatus.SENDING}:
        return Badge(label, "warning")
    if status in {CampaignStatus.FAILED, CampaignStatus.CANCELLED}:
        return Badge(label, "danger")
    return Badge(label, "neutral")


def campaign_timing(campaign: Campaign) -> tuple[str, object | None]:
    if campaign.sent_at:
        return "Sent", campaign.sent_at
    if campaign.scheduled_at:
        return "Scheduled", campaign.scheduled_at
    if campaign.status in {CampaignStatus.QUEUED, CampaignStatus.SNAPSHOTTING, CampaignStatus.SENDING}:
        return "Queued", campaign.updated_at
    return "Updated", campaign.updated_at


def campaign_list_rows(campaigns) -> list[CampaignListRow]:
    rows = []
    for campaign in campaigns:
        timing_label, timing_value = campaign_timing(campaign)
        progress = campaign_send_progress(campaign)
        rows.append(
            CampaignListRow(
                campaign=campaign,
                badge=campaign_status_badge(campaign.status),
                timing_label=timing_label,
                timing_value=timing_value,
                progress=progress,
            )
        )
    return rows


def campaign_send_progress(campaign: Campaign) -> CampaignSendProgress:
    recipients = CampaignRecipient.objects.filter(campaign=campaign)
    queued_count = recipients.filter(status=CampaignRecipientStatus.PENDING).count()
    processed_count = recipients.filter(status__in=PROCESSED_RECIPIENT_STATUSES).count()
    sent_window = recipients.filter(sent_at__isnull=False).aggregate(
        sent_count=Count("id"),
        started_at=Min("sent_at"),
        ended_at=Max("sent_at"),
    )
    sent_count = sent_window["sent_count"] or 0
    started_at = sent_window["started_at"]
    ended_at = sent_window["ended_at"]
    duration_seconds = None
    per_second = ""
    per_minute = ""
    if started_at and ended_at and sent_count:
        duration_seconds = max(int((ended_at - started_at).total_seconds()), 1)
        per_second = f"{sent_count / duration_seconds:.2f}/sec"
        per_minute = f"{(sent_count / duration_seconds) * 60:.1f}/min"

    return CampaignSendProgress(
        queued_count=queued_count,
        processed_count=processed_count,
        sent_count=sent_count,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        duration_label=format_duration(duration_seconds),
        per_second=per_second,
        per_minute=per_minute,
    )


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    minutes, remaining_seconds = divmod(seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if remaining_minutes:
        parts.append(f"{remaining_minutes}m")
    if remaining_seconds or not parts:
        parts.append(f"{remaining_seconds}s")
    return " ".join(parts)


def campaign_stats(campaign: Campaign) -> list[Stat]:
    failed_count = CampaignRecipient.objects.filter(
        campaign=campaign,
        status=CampaignRecipientStatus.FAILED,
    ).count()
    recipient_count = campaign.recipient_count
    sent_count = campaign.sent_count
    progress = campaign_send_progress(campaign)

    return [
        Stat("recipients", "Recipients", recipient_count),
        Stat("queued", "Queued", progress.queued_count),
        Stat("processed", "Processed", progress.processed_count, rate(progress.processed_count, recipient_count)),
        Stat("sent", "Sent", sent_count, rate(sent_count, recipient_count)),
        Stat("skipped", "Skipped", campaign.skipped_count, rate(campaign.skipped_count, recipient_count)),
        Stat("delivered", "Delivered", campaign.delivered_count, rate(campaign.delivered_count, sent_count)),
        Stat("unique_opens", "Unique opens", campaign.unique_open_count, rate(campaign.unique_open_count, sent_count)),
        Stat("total_opens", "Total opens", campaign.open_count),
        Stat(
            "unique_clicks", "Unique clicks", campaign.unique_click_count, rate(campaign.unique_click_count, sent_count)
        ),
        Stat("total_clicks", "Total clicks", campaign.click_count),
        Stat("unsubscribes", "Unsubscribes", campaign.unsubscribe_count, rate(campaign.unsubscribe_count, sent_count)),
        Stat("bounces", "Bounces", campaign.bounce_count, rate(campaign.bounce_count, sent_count)),
        Stat("complaints", "Complaints", campaign.complaint_count, rate(campaign.complaint_count, sent_count)),
        Stat("failures", "Failures", failed_count, rate(failed_count, recipient_count)),
    ]


def campaign_queryset(client: Client | None = None) -> QuerySet[Campaign]:
    queryset = Campaign.objects.select_related("client", "audience", "audience__organization")
    if client is not None:
        queryset = queryset.filter(client=client)
    return queryset.order_by("-created_at", "-id")


def campaign_recent_events(campaign: Campaign) -> QuerySet[EmailEvent]:
    return (
        EmailEvent.objects.filter(campaign=campaign)
        .select_related("contact", "client", "audience", "campaign_recipient")
        .order_by("-created_at", "-id")
    )


def campaign_recipient_queryset(campaign: Campaign, filter_key: str = "") -> QuerySet[CampaignRecipient]:
    queryset = (
        CampaignRecipient.objects.filter(campaign=campaign)
        .select_related("contact")
        .order_by("contact__normalized_email", "id")
    )
    filter_condition = RECIPIENT_FILTERS.get(filter_key)
    if filter_condition is not None:
        queryset = queryset.filter(filter_condition)
    return queryset


def contact_search_queryset(query: str) -> QuerySet[Contact]:
    filters = ContactExplorerFilters(query=query)
    if not filters.query.strip():
        return Contact.objects.none()
    return contact_explorer_queryset(filters)


def contact_explorer_options(client: Client | None = None):
    audience_queryset = Audience.objects.select_related("organization")
    client_queryset = Client.objects.select_related("organization")
    tag_queryset = Tag.objects.select_related("audience")
    if client is not None:
        audience_queryset = audience_queryset.filter(organization=client.organization)
        client_queryset = client_queryset.filter(pk=client.pk)
        tag_queryset = tag_queryset.filter(audience__organization=client.organization)
    return {
        "audiences": audience_queryset.order_by("organization__slug", "slug"),
        "clients": client_queryset.order_by("organization__slug", "slug"),
        "tags": tag_queryset.order_by("audience__slug", "slug"),
        "subscription_statuses": choices_from_text_choices(SubscriptionStatus),
        "verified_states": [Choice(value=key, label=label) for key, label in VERIFIED_FILTER_LABELS.items()],
        "email_validation_statuses": choices_from_text_choices(EmailValidationStatus),
        "suppression_states": [Choice(value=key, label=label) for key, label in SUPPRESSION_FILTER_LABELS.items()],
        "campaign_statuses": choices_from_text_choices(CampaignRecipientStatus),
        "skip_reasons": choices_from_text_choices(CampaignRecipientSkipReason),
        "engagement_states": [Choice(value=key, label=label) for key, label in ENGAGEMENT_FILTER_LABELS.items()],
    }


def active_contact_filters(filters: ContactExplorerFilters) -> list[ActiveFilter]:
    chips = []
    if filters.query:
        chips.append(ActiveFilter("Email", filters.query))
    if filters.audience_id:
        audience = Audience.objects.filter(pk=filters.audience_id).first()
        chips.append(ActiveFilter("Audience", audience.name if audience else str(filters.audience_id)))
    if filters.client_id:
        client = Client.objects.filter(pk=filters.client_id).first()
        chips.append(ActiveFilter("Client", client.name if client else str(filters.client_id)))
    if filters.subscription_status:
        chips.append(ActiveFilter("Subscription", SubscriptionStatus(filters.subscription_status).label))
    if filters.verified_state:
        chips.append(ActiveFilter("Verification", VERIFIED_FILTER_LABELS[filters.verified_state]))
    if filters.email_validation_status:
        chips.append(ActiveFilter("Validation", EmailValidationStatus(filters.email_validation_status).label))
    if filters.suppression_state:
        chips.append(ActiveFilter("Suppression", SUPPRESSION_FILTER_LABELS[filters.suppression_state]))
    if filters.campaign_status:
        chips.append(ActiveFilter("Campaign", CampaignRecipientStatus(filters.campaign_status).label))
    if filters.skip_reason:
        chips.append(ActiveFilter("Skip reason", CampaignRecipientSkipReason(filters.skip_reason).label))
    if filters.engagement:
        value = ENGAGEMENT_FILTER_LABELS[filters.engagement]
        if filters.engagement == "inactive_since" and filters.inactive_since:
            value = f"{value} {filters.inactive_since.isoformat()}"
        chips.append(ActiveFilter("Engagement", value))
    chips.extend(ActiveFilter("Includes tag", tag) for tag in filters.include_tags)
    chips.extend(ActiveFilter("Excludes tag", tag) for tag in filters.exclude_tags)
    return chips


def parse_contact_explorer_filters(params, *, forced_audience_id=None, forced_client_id=None) -> ContactExplorerFilters:
    inactive_since = parse_date(params.get("inactive_since", ""))
    engagement = params.get("engagement", "")
    if engagement == "inactive_since" and inactive_since is None:
        engagement = ""

    return ContactExplorerFilters(
        query=params.get("q", "").strip(),
        audience_id=forced_audience_id or positive_int(params.get("audience")),
        client_id=forced_client_id or positive_int(params.get("client")),
        include_tags=tuple(nonempty_values(params.getlist("include_tags"))),
        exclude_tags=tuple(nonempty_values(params.getlist("exclude_tags"))),
        subscription_status=valid_choice(params.get("subscription_status", ""), SubscriptionStatus),
        verified_state=params.get("verified", "") if params.get("verified", "") in VERIFIED_FILTER_LABELS else "",
        email_validation_status=valid_choice(params.get("email_validation_status", ""), EmailValidationStatus),
        suppression_state=params.get("suppression", "")
        if params.get("suppression", "") in SUPPRESSION_FILTER_LABELS
        else "",
        campaign_status=valid_choice(params.get("campaign_status", ""), CampaignRecipientStatus),
        skip_reason=valid_choice(params.get("skip_reason", ""), CampaignRecipientSkipReason),
        engagement=engagement if engagement in ENGAGEMENT_FILTER_LABELS else "",
        inactive_since=inactive_since,
    )


def positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def valid_choice(value, text_choices) -> str:
    values = {choice.value for choice in text_choices}
    return value if value in values else ""


def nonempty_values(values):
    return [value for value in values if str(value).strip()]


def contact_explorer_queryset(filters: ContactExplorerFilters) -> QuerySet[Contact]:
    queryset = Contact.objects.all()

    if filters.query:
        normalized = normalize_email(filters.query)
        queryset = queryset.filter(Q(normalized_email__icontains=normalized) | Q(email__icontains=filters.query))
    subscription_scope = subscription_scope_queryset(filters)
    if filters.audience_id or filters.client_id or filters.subscription_status:
        queryset = queryset.filter(Exists(subscription_scope))
    if filters.verified_state == "verified":
        queryset = queryset.filter(verified_at__isnull=False)
    elif filters.verified_state == "unverified":
        queryset = queryset.filter(verified_at__isnull=True)
    if filters.email_validation_status:
        queryset = queryset.filter(email_validation_status=filters.email_validation_status)
    if filters.suppression_state == "global_unsubscribed":
        queryset = queryset.filter(global_unsubscribed_at__isnull=False)
    elif filters.suppression_state == "hard_bounced":
        queryset = queryset.filter(hard_bounced_at__isnull=False)
    elif filters.suppression_state == "complained":
        queryset = queryset.filter(complained_at__isnull=False)
    for tag_slug in filters.include_tags:
        queryset = queryset.filter(Exists(contact_tag_scope_queryset(filters).filter(tag__slug=tag_slug)))
    for tag_slug in filters.exclude_tags:
        queryset = queryset.exclude(Exists(contact_tag_scope_queryset(filters).filter(tag__slug=tag_slug)))
    if filters.campaign_status:
        queryset = queryset.filter(campaign_recipients__status=filters.campaign_status)
        if filters.client_id:
            queryset = queryset.filter(campaign_recipients__campaign__client_id=filters.client_id)
    if filters.skip_reason:
        queryset = queryset.filter(campaign_recipients__skip_reason=filters.skip_reason)
        if filters.client_id:
            queryset = queryset.filter(campaign_recipients__campaign__client_id=filters.client_id)

    queryset = apply_engagement_filter(queryset, filters)
    return (
        queryset.prefetch_related(
            "subscriptions__audience",
            "subscriptions__client",
            "contact_tags__tag__audience",
        )
        .annotate(
            campaign_recipient_count=Count("campaign_recipients", distinct=True),
            transactional_message_count=Count("transactional_messages", distinct=True),
            last_campaign_sent_at=Max("campaign_recipients__sent_at"),
            last_transactional_sent_at=Max("transactional_messages__sent_at"),
            last_campaign_opened_at=Max("campaign_recipients__first_opened_at"),
            last_transactional_opened_at=Max("transactional_messages__first_opened_at"),
            last_campaign_clicked_at=Max("campaign_recipients__first_clicked_at"),
            last_transactional_clicked_at=Max("transactional_messages__first_clicked_at"),
        )
        .distinct()
        .order_by("normalized_email", "id")
    )


def subscription_scope_queryset(filters: ContactExplorerFilters):
    scope = {"contact_id": OuterRef("pk")}
    if filters.audience_id:
        scope["audience_id"] = filters.audience_id
    if filters.client_id:
        scope["client_id"] = filters.client_id
    if filters.subscription_status:
        scope["status"] = filters.subscription_status
    return Subscription.objects.filter(**scope)


def contact_tag_scope_queryset(filters: ContactExplorerFilters):
    scope = {"contact_id": OuterRef("pk")}
    if filters.audience_id:
        scope["tag__audience_id"] = filters.audience_id
    return ContactTag.objects.filter(**scope)


def apply_engagement_filter(queryset: QuerySet[Contact], filters: ContactExplorerFilters) -> QuerySet[Contact]:
    if not filters.engagement:
        return queryset

    campaign_sent = campaign_sent_queryset(filters)
    campaign_open = campaign_open_queryset(filters)
    campaign_click = campaign_click_queryset(filters)
    tx_sent = transactional_sent_queryset(filters)
    tx_open = transactional_open_queryset(filters)
    tx_click = transactional_click_queryset(filters)

    if filters.engagement == "not_opened":
        return queryset.filter(Exists(campaign_sent.filter(first_opened_at__isnull=True)))
    if filters.engagement == "not_clicked":
        return queryset.filter(Exists(campaign_sent.filter(first_clicked_at__isnull=True)))
    if filters.engagement == "opened_not_clicked":
        return queryset.filter(Exists(campaign_open)).exclude(Exists(campaign_click))
    if filters.engagement == "never_opened":
        return queryset.filter(Q(Exists(campaign_sent)) | Q(Exists(tx_sent))).exclude(
            Q(Exists(campaign_open)) | Q(Exists(tx_open))
        )
    if filters.engagement == "never_clicked":
        return queryset.filter(Q(Exists(campaign_sent)) | Q(Exists(tx_sent))).exclude(
            Q(Exists(campaign_click)) | Q(Exists(tx_click))
        )
    if filters.engagement == "inactive_since" and filters.inactive_since:
        cutoff = timezone.make_aware(datetime.combine(filters.inactive_since, time.min))
        return queryset.filter(
            Q(Exists(campaign_sent.filter(sent_at__lt=cutoff))) | Q(Exists(tx_sent.filter(sent_at__lt=cutoff)))
        ).exclude(
            Q(Exists(campaign_open.filter(first_opened_at__gte=cutoff)))
            | Q(Exists(campaign_click.filter(first_clicked_at__gte=cutoff)))
            | Q(Exists(tx_open.filter(first_opened_at__gte=cutoff)))
            | Q(Exists(tx_click.filter(first_clicked_at__gte=cutoff)))
        )
    return queryset


def campaign_scope_filter(filters: ContactExplorerFilters):
    scope = {"contact_id": OuterRef("pk")}
    if filters.audience_id:
        scope["campaign__audience_id"] = filters.audience_id
    if filters.client_id:
        scope["campaign__client_id"] = filters.client_id
    return scope


def transactional_scope_filter(filters: ContactExplorerFilters):
    scope = {"contact_id": OuterRef("pk")}
    if filters.client_id:
        scope["client_id"] = filters.client_id
    return scope


def campaign_sent_queryset(filters: ContactExplorerFilters):
    return CampaignRecipient.objects.filter(**campaign_scope_filter(filters)).filter(
        Q(status=CampaignRecipientStatus.SENT) | Q(delivered_at__isnull=False) | Q(sent_at__isnull=False)
    )


def campaign_open_queryset(filters: ContactExplorerFilters):
    return CampaignRecipient.objects.filter(**campaign_scope_filter(filters), first_opened_at__isnull=False)


def campaign_click_queryset(filters: ContactExplorerFilters):
    return CampaignRecipient.objects.filter(**campaign_scope_filter(filters), first_clicked_at__isnull=False)


def transactional_sent_queryset(filters: ContactExplorerFilters):
    return TransactionalMessage.objects.filter(**transactional_scope_filter(filters)).filter(
        Q(status=TransactionalMessageStatus.SENT) | Q(delivered_at__isnull=False) | Q(sent_at__isnull=False)
    )


def transactional_open_queryset(filters: ContactExplorerFilters):
    return TransactionalMessage.objects.filter(**transactional_scope_filter(filters), first_opened_at__isnull=False)


def transactional_click_queryset(filters: ContactExplorerFilters):
    return TransactionalMessage.objects.filter(**transactional_scope_filter(filters), first_clicked_at__isnull=False)


def contact_result_rows(
    contacts, *, audience: Audience | None = None, client: Client | None = None
) -> list[ContactResultRow]:
    rows = []
    contact_ids = [contact.id for contact in contacts]
    recent_issues = recent_contact_issues(contact_ids, audience=audience, client=client)
    for contact in contacts:
        subscriptions = scoped_subscriptions(contact.subscriptions.all(), audience)
        contact_tags = scoped_contact_tags(contact.contact_tags.all(), audience)
        rows.append(
            ContactResultRow(
                contact=contact,
                verification_badge=verification_badge(contact),
                validation_badge=validation_badge(contact),
                subscription_badge=subscription_badge(contact, subscriptions),
                subscription_summary=subscription_summary(subscriptions),
                tag_summary=tag_summary(contact_tags),
                last_sent_at=max_date(contact.last_campaign_sent_at, contact.last_transactional_sent_at),
                last_opened_at=max_date(contact.last_campaign_opened_at, contact.last_transactional_opened_at),
                last_clicked_at=max_date(contact.last_campaign_clicked_at, contact.last_transactional_clicked_at),
                recent_issue=recent_issues.get(contact.id, ""),
            )
        )
    return rows


def scoped_subscriptions(subscriptions, audience: Audience | None):
    if audience is None:
        return subscriptions
    return [subscription for subscription in subscriptions if subscription.audience_id == audience.id]


def scoped_contact_tags(contact_tags, audience: Audience | None):
    if audience is None:
        return contact_tags
    return [membership for membership in contact_tags if membership.tag.audience_id == audience.id]


def max_date(*values):
    present = [value for value in values if value is not None]
    return max(present) if present else None


def subscription_summary(subscriptions) -> str:
    parts = []
    for subscription in subscriptions:
        client_label = subscription.client.slug if subscription.client_id else "audience"
        parts.append(f"{subscription.audience.slug}/{client_label}: {subscription.status}")
    return "; ".join(parts) or "-"


def tag_summary(contact_tags) -> str:
    labels = [f"{membership.tag.audience.slug}/{membership.tag.slug}" for membership in contact_tags]
    return ", ".join(labels) or "-"


def recent_contact_issues(
    contact_ids, *, audience: Audience | None = None, client: Client | None = None
) -> dict[int, str]:
    if not contact_ids:
        return {}
    issue_statuses = [
        CampaignRecipientStatus.FAILED,
        CampaignRecipientStatus.SKIPPED,
        CampaignRecipientStatus.BOUNCED,
        CampaignRecipientStatus.COMPLAINED,
        CampaignRecipientStatus.UNSUBSCRIBED,
    ]
    issues = CampaignRecipient.objects.filter(contact_id__in=contact_ids, status__in=issue_statuses)
    if audience is not None:
        issues = issues.filter(campaign__audience=audience)
    if client is not None:
        issues = issues.filter(campaign__client=client)
    issues = issues.select_related("campaign").order_by("contact_id", "-created_at", "-id")
    result = {}
    for recipient in issues:
        if recipient.contact_id in result:
            continue
        label = recipient.get_skip_reason_display() if recipient.skip_reason else recipient.get_status_display()
        result[recipient.contact_id] = f"{label}: {recipient.campaign.subject}"
    return result


def contact_detail_queryset() -> QuerySet[Contact]:
    return Contact.objects.prefetch_related(
        "subscriptions__audience__organization",
        "subscriptions__client",
        "contact_tags__tag__audience",
    )


def contact_campaign_history(contact: Contact, client: Client | None = None) -> QuerySet[CampaignRecipient]:
    queryset = CampaignRecipient.objects.filter(contact=contact).select_related(
        "campaign",
        "campaign__client",
        "campaign__audience",
    )
    if client is not None:
        queryset = queryset.filter(campaign__client=client)
    return queryset.order_by("-created_at", "-id")


def contact_transactional_history(contact: Contact, client: Client | None = None):
    queryset = contact.transactional_messages.select_related("client")
    if client is not None:
        queryset = queryset.filter(client=client)
    return queryset.order_by("-created_at", "-id").only(
        "id",
        "client__name",
        "client__slug",
        "template_key",
        "status",
        "subject",
        "ses_message_id",
        "created_at",
        "sent_at",
        "delivered_at",
        "first_opened_at",
        "first_clicked_at",
        "open_count",
        "click_count",
        "metadata",
        "last_error",
    )


def contact_detail_context(contact: Contact, client: Client | None = None) -> ContactDetailContext:
    subscriptions = contact.subscriptions.select_related("audience", "client").order_by(
        "audience__slug",
        "client__slug",
        "id",
    )
    if client is not None:
        subscriptions = subscriptions.filter(client=client)
    eligibility = eligibility_items(contact, subscriptions)
    contact_tags = contact.contact_tags.select_related("tag", "tag__audience").order_by(
        "tag__audience__slug",
        "tag__slug",
    )
    if client is not None:
        contact_tags = contact_tags.filter(tag__audience__organization=client.organization)
    return ContactDetailContext(
        eligibility=eligibility,
        subscriptions=subscriptions,
        contact_tags=contact_tags,
        verification_badge=verification_badge(contact),
        validation_badge=validation_badge(contact),
        subscription_badge=subscription_badge(contact, subscriptions),
        sendability=sendability_summary(eligibility),
        metrics=contact_metrics(contact, client),
        recent_activity=recent_contact_activity(contact, client),
    )


def verification_badge(contact: Contact) -> Badge:
    if contact.verified_at:
        return Badge("Verified", "success")
    return Badge("Unverified", "warning")


def validation_badge(contact: Contact) -> Badge:
    label = contact.get_email_validation_status_display()
    if contact.email_validation_status in {
        EmailValidationStatus.VALID,
        EmailValidationStatus.EXTERNALLY_VALIDATED,
    }:
        return Badge(label, "success")
    if contact.email_validation_status in {
        EmailValidationStatus.RISKY,
        EmailValidationStatus.UNKNOWN,
    }:
        return Badge(label, "warning" if contact.email_validation_status == EmailValidationStatus.RISKY else "neutral")
    return Badge(label, "danger")


def subscription_badge(contact: Contact, subscriptions) -> Badge:
    if contact.global_unsubscribed_at:
        return Badge("Globally unsubscribed", "danger")
    if contact.complained_at:
        return Badge("Complained", "danger")
    if contact.hard_bounced_at:
        return Badge("Hard bounced", "danger")
    statuses = [subscription.status for subscription in subscriptions]
    if SubscriptionStatus.SUBSCRIBED in statuses:
        return Badge("Subscribed", "success")
    if SubscriptionStatus.UNSUBSCRIBED in statuses:
        return Badge("Unsubscribed", "danger")
    if SubscriptionStatus.PENDING in statuses:
        return Badge("Pending", "warning")
    return Badge("No subscriptions", "neutral")


def sendability_summary(eligibility: list[EligibilityItem]) -> SendabilitySummary:
    marketing_can_send = any(item.can_send_marketing for item in eligibility)
    transactional_can_send = any(item.can_send_transactional for item in eligibility)
    return SendabilitySummary(
        marketing_badge=Badge(
            "Can send marketing" if marketing_can_send else "Cannot send marketing",
            "success" if marketing_can_send else "danger",
        ),
        marketing_reasons=unique_reasons(
            reason
            for item in eligibility
            for reason in item.marketing_reasons
            if marketing_can_send is False or reason != "eligible"
        )
        or ("eligible in at least one subscription scope",),
        transactional_badge=Badge(
            "Can send transactional" if transactional_can_send else "Cannot send transactional",
            "success" if transactional_can_send else "danger",
        ),
        transactional_reasons=unique_reasons(
            reason
            for item in eligibility
            for reason in item.transactional_reasons
            if transactional_can_send is False or reason != "eligible"
        )
        or ("eligible",),
    )


def unique_reasons(reasons) -> tuple[str, ...]:
    seen = []
    for reason in reasons:
        if reason and reason not in seen:
            seen.append(reason)
    return tuple(seen)


def contact_metrics(contact: Contact, client: Client | None = None) -> list[ContactMetric]:
    campaign_rows = CampaignRecipient.objects.filter(contact=contact)
    transactional_rows = contact.transactional_messages.all()
    event_rows = EmailEvent.objects.filter(contact=contact)
    if client is not None:
        campaign_rows = campaign_rows.filter(campaign__client=client)
        transactional_rows = transactional_rows.filter(client=client)
        event_rows = event_rows.filter(client=client)
    return [
        ContactMetric(
            "Last sent",
            latest_datetime(
                campaign_rows.aggregate(value=Max("sent_at"))["value"],
                transactional_rows.aggregate(value=Max("sent_at"))["value"],
            ),
            "success",
        ),
        ContactMetric(
            "Last opened",
            latest_datetime(
                campaign_rows.aggregate(value=Max("first_opened_at"))["value"],
                transactional_rows.aggregate(value=Max("first_opened_at"))["value"],
            ),
            "success",
        ),
        ContactMetric(
            "Last clicked",
            latest_datetime(
                campaign_rows.aggregate(value=Max("first_clicked_at"))["value"],
                transactional_rows.aggregate(value=Max("first_clicked_at"))["value"],
            ),
            "success",
        ),
        ContactMetric(
            "Last bounce",
            latest_datetime(
                contact.hard_bounced_at,
                event_rows.filter(event_type=EmailEventType.BOUNCE).aggregate(value=Max("created_at"))["value"],
            ),
            "danger",
        ),
        ContactMetric(
            "Last complaint",
            latest_datetime(
                contact.complained_at,
                event_rows.filter(event_type=EmailEventType.COMPLAINT).aggregate(value=Max("created_at"))["value"],
            ),
            "danger",
        ),
        ContactMetric(
            "Last unsubscribe",
            latest_datetime(
                contact.global_unsubscribed_at,
                event_rows.filter(event_type=EmailEventType.UNSUBSCRIBE).aggregate(value=Max("created_at"))["value"],
            ),
            "danger",
        ),
    ]


def latest_datetime(*values):
    present = [value for value in values if value is not None]
    return max(present) if present else None


def recent_contact_activity(contact: Contact, client: Client | None = None) -> list[ContactActivity]:
    rows: list[ContactActivity] = []
    for event in contact_event_timeline(contact, client)[:8]:
        rows.append(
            ContactActivity(
                event.created_at,
                Badge(event.get_event_type_display(), event_tone(event.event_type)),
                event_context(event),
                event.url or "Email event recorded",
            )
        )
    for recipient in contact_campaign_history(contact, client)[:5]:
        rows.append(
            ContactActivity(
                recipient.sent_at or recipient.created_at,
                Badge(recipient.get_status_display(), delivery_tone(recipient.status)),
                f"Campaign: {recipient.campaign.subject}",
                f"{recipient.campaign.client.name} / {recipient.campaign.audience.name}",
                href=f"/campaigns/{recipient.campaign_id}/#recipient-{recipient.id}",
                metadata=recipient.last_error or recipient.get_skip_reason_display(),
            )
        )
    for message in contact_transactional_history(contact, client)[:5]:
        rows.append(
            ContactActivity(
                message.sent_at or message.created_at,
                Badge(message.get_status_display(), delivery_tone(message.status)),
                f"Transactional: {message.template_key}",
                message.subject,
                metadata=message.last_error or metadata_summary(message.metadata),
            )
        )
    rows.sort(key=lambda row: (row.occurred_at is not None, row.occurred_at), reverse=True)
    return rows[:10]


def event_tone(event_type: str) -> str:
    if event_type in {
        EmailEventType.DELIVERED,
        EmailEventType.OPEN,
        EmailEventType.CLICK,
        EmailEventType.SENT,
        EmailEventType.SUBSCRIBE,
    }:
        return "success"
    if event_type in {
        EmailEventType.BOUNCE,
        EmailEventType.COMPLAINT,
        EmailEventType.UNSUBSCRIBE,
        EmailEventType.FAILED,
    }:
        return "danger"
    if event_type in {EmailEventType.SKIPPED, EmailEventType.QUEUED}:
        return "warning"
    return "neutral"


def delivery_tone(status: str) -> str:
    if status in {CampaignRecipientStatus.SENT, TransactionalMessageStatus.SENT}:
        return "success"
    if status in {
        CampaignRecipientStatus.BOUNCED,
        CampaignRecipientStatus.COMPLAINED,
        CampaignRecipientStatus.UNSUBSCRIBED,
        CampaignRecipientStatus.FAILED,
        TransactionalMessageStatus.BOUNCED,
        TransactionalMessageStatus.COMPLAINED,
        TransactionalMessageStatus.FAILED,
    }:
        return "danger"
    if status in {
        CampaignRecipientStatus.SKIPPED,
        TransactionalMessageStatus.SKIPPED,
        CampaignRecipientStatus.PENDING,
        TransactionalMessageStatus.QUEUED,
    }:
        return "warning"
    return "neutral"


def eligibility_items(contact: Contact, subscriptions) -> list[EligibilityItem]:
    rows = []
    for subscription in subscriptions:
        rows.append(eligibility_item(contact, subscription))
    if not rows:
        tx_reasons = transactional_reasons(contact)
        rows.append(
            EligibilityItem(
                scope="No audience/client subscription",
                can_send_marketing=False,
                marketing_reasons=("not subscribed",),
                can_send_transactional=not tx_reasons,
                transactional_reasons=tuple(tx_reasons or ["eligible"]),
            )
        )
    return rows


def eligibility_item(contact: Contact, subscription: Subscription) -> EligibilityItem:
    scope = f"{subscription.audience.name} / {subscription.client.name if subscription.client_id else 'Audience-wide'}"
    marketing = list(marketing_reasons(contact, subscription))
    tx_reasons = transactional_reasons(contact)
    return EligibilityItem(
        scope=scope,
        can_send_marketing=not marketing,
        marketing_reasons=tuple(marketing or ["eligible"]),
        can_send_transactional=not tx_reasons,
        transactional_reasons=tuple(tx_reasons or ["eligible"]),
    )


def marketing_reasons(contact: Contact, subscription: Subscription):
    if contact.verified_at is None:
        yield "unverified"
    if has_invalid_email_validation(contact):
        yield f"invalid email validation: {contact.get_email_validation_status_display()}"
    if contact.global_unsubscribed_at is not None:
        yield "global unsubscribe"
    if contact.hard_bounced_at is not None:
        yield "hard bounce"
    if contact.complained_at is not None:
        yield "complaint"
    if subscription.status == SubscriptionStatus.UNSUBSCRIBED:
        if subscription.client_id:
            yield "client unsubscribe"
        else:
            yield "audience unsubscribe"
    elif subscription.status != SubscriptionStatus.SUBSCRIBED:
        yield f"{subscription.get_status_display().lower()} / not subscribed"


def transactional_reasons(contact: Contact):
    reasons = []
    if contact.hard_bounced_at is not None:
        reasons.append("hard bounce")
    if contact.complained_at is not None:
        reasons.append("complaint")
    return tuple(reasons)


def contact_event_timeline(contact: Contact, client: Client | None = None) -> QuerySet[EmailEvent]:
    queryset = EmailEvent.objects.filter(contact=contact).select_related(
        "campaign",
        "campaign__client",
        "campaign__audience",
        "campaign_recipient",
        "transactional_message",
        "client",
        "audience",
    )
    if client is not None:
        queryset = queryset.filter(client=client)
    return queryset.order_by("-created_at", "-id")


def event_context(event: EmailEvent) -> str:
    if event.campaign_id:
        return f"Campaign: {event.campaign.subject}"
    if event.transactional_message_id:
        return f"Transactional: {event.transactional_message.template_key}"
    return "Contact event"


def metadata_summary(metadata) -> str:
    if not metadata:
        return ""
    if not isinstance(metadata, dict):
        return str(metadata)

    preferred = []
    for key in ("reason", "error", "scope", "ses_message_id", "bounce_type", "complaint_feedback_type"):
        value = metadata.get(key)
        if value not in (None, "", [], {}):
            preferred.append(f"{key}: {value}")

    if preferred:
        return "; ".join(preferred)
    return json.dumps(metadata, sort_keys=True)[:240]


def audience_queryset(client: Client | None = None) -> QuerySet[Audience]:
    queryset = Audience.objects.select_related("organization")
    if client is not None:
        queryset = queryset.filter(organization=client.organization)
    return queryset.annotate(
        subscription_count=Count("subscriptions", distinct=True),
        contact_count=Count("subscriptions__contact", distinct=True),
        campaign_count=Count("campaigns", distinct=True),
        subscribed_count=Count(
            "subscriptions",
            filter=Q(subscriptions__status=SubscriptionStatus.SUBSCRIBED),
            distinct=True,
        ),
    ).order_by("organization__slug", "slug")


def audience_detail_queryset() -> QuerySet[Audience]:
    return Audience.objects.select_related("organization")


def audience_list_rows(audiences, client: Client | None = None) -> list[AudienceListRow]:
    return [
        AudienceListRow(
            audience=audience,
            member_count=audience.contact_count,
            subscribed_count=audience.subscribed_count,
            inactive_count=audience_inactive_count(audience),
            suppressed_count=audience_suppressed_count(audience),
            campaign_count=audience.campaign_count,
            recent_campaign=campaign_queryset(client).filter(audience=audience).order_by("-updated_at", "-id").first(),
            recent_event=(
                EmailEvent.objects.filter(audience=audience, client=client).order_by("-created_at", "-id").first()
                if client is not None
                else EmailEvent.objects.filter(audience=audience).order_by("-created_at", "-id").first()
            ),
        )
        for audience in audiences
    ]


def audience_contacts(audience: Audience) -> QuerySet[Contact]:
    return Contact.objects.filter(subscriptions__audience=audience).distinct()


def audience_inactive_count(audience: Audience) -> int:
    contacts = audience_contacts(audience)
    sent_contacts = contacts.filter(
        Q(campaign_recipients__campaign__audience=audience, campaign_recipients__sent_at__isnull=False)
        | Q(campaign_recipients__campaign__audience=audience, campaign_recipients__status=CampaignRecipientStatus.SENT)
    )
    engaged_contacts = contacts.filter(
        Q(campaign_recipients__campaign__audience=audience, campaign_recipients__first_opened_at__isnull=False)
        | Q(campaign_recipients__campaign__audience=audience, campaign_recipients__first_clicked_at__isnull=False)
        | Q(email_events__audience=audience, email_events__event_type__in=[EmailEventType.OPEN, EmailEventType.CLICK])
    )
    return sent_contacts.exclude(pk__in=engaged_contacts.values("pk")).distinct().count()


def audience_suppressed_count(audience: Audience) -> int:
    return (
        audience_contacts(audience)
        .filter(
            Q(global_unsubscribed_at__isnull=False)
            | Q(hard_bounced_at__isnull=False)
            | Q(complained_at__isnull=False)
            | Q(subscriptions__audience=audience, subscriptions__status=SubscriptionStatus.UNSUBSCRIBED)
        )
        .distinct()
        .count()
    )


def audience_summary(audience: Audience, client: Client | None = None) -> list[Stat]:
    contacts = audience_contacts(audience)
    subscriptions = Subscription.objects.filter(audience=audience)
    if client is not None:
        contacts = contacts.filter(subscriptions__client=client).distinct()
        subscriptions = subscriptions.filter(client=client)
    return [
        Stat("members", "Members", contacts.count()),
        Stat("subscribed", "Subscribed", subscriptions.filter(status=SubscriptionStatus.SUBSCRIBED).count()),
        Stat("pending", "Pending", subscriptions.filter(status=SubscriptionStatus.PENDING).count()),
        Stat(
            "unsubscribed",
            "Unsubscribed",
            subscriptions.filter(status=SubscriptionStatus.UNSUBSCRIBED).count(),
        ),
        Stat("verified", "Verified", contacts.filter(verified_at__isnull=False).count()),
        Stat("unverified", "Unverified", contacts.filter(verified_at__isnull=True).count()),
        Stat("inactive", "Inactive", audience_inactive_count(audience)),
        Stat(
            "global_unsubscribed", "Global unsubscribed", contacts.filter(global_unsubscribed_at__isnull=False).count()
        ),
        Stat("hard_bounced", "Hard bounced", contacts.filter(hard_bounced_at__isnull=False).count()),
        Stat("complained", "Complained", contacts.filter(complained_at__isnull=False).count()),
        Stat(
            "opened",
            "Opened",
            contacts.filter(
                Q(campaign_recipients__campaign__audience=audience, campaign_recipients__first_opened_at__isnull=False)
                | Q(email_events__audience=audience, email_events__event_type=EmailEventType.OPEN)
            )
            .distinct()
            .count(),
        ),
        Stat(
            "clicked",
            "Clicked",
            contacts.filter(
                Q(campaign_recipients__campaign__audience=audience, campaign_recipients__first_clicked_at__isnull=False)
                | Q(email_events__audience=audience, email_events__event_type=EmailEventType.CLICK)
            )
            .distinct()
            .count(),
        ),
    ]


def count_by_field(queryset, field, *, choices=None):
    raw_counts = dict(queryset.values_list(field).annotate(count=Count("id")))
    if choices:
        return [(label, raw_counts.get(value, 0)) for value, label in choices]
    return sorted(raw_counts.items())


def audience_breakdowns(audience: Audience, client: Client | None = None):
    contacts = audience_contacts(audience)
    campaign_recipients = CampaignRecipient.objects.filter(campaign__audience=audience)
    if client is not None:
        contacts = contacts.filter(subscriptions__client=client).distinct()
        campaign_recipients = campaign_recipients.filter(campaign__client=client)
    return {
        "validation": count_by_field(contacts, "email_validation_status", choices=EmailValidationStatus.choices),
        "tags": Tag.objects.filter(audience=audience)
        .annotate(count=Count("contact_tags", distinct=True))
        .order_by("slug"),
        "campaign_statuses": count_by_field(
            campaign_recipients,
            "status",
            choices=CampaignRecipientStatus.choices,
        ),
        "skip_reasons": count_by_field(
            campaign_recipients.exclude(skip_reason=""),
            "skip_reason",
            choices=CampaignRecipientSkipReason.choices,
        ),
    }


def audience_campaign_history(audience: Audience, client: Client | None = None) -> QuerySet[Campaign]:
    queryset = Campaign.objects.filter(audience=audience).select_related("client")
    if client is not None:
        queryset = queryset.filter(client=client)
    return queryset.order_by("-created_at", "-id")


def audience_recent_events(
    audience: Audience, event_type: str = "", client: Client | None = None
) -> QuerySet[EmailEvent]:
    queryset = EmailEvent.objects.filter(audience=audience).select_related(
        "contact",
        "campaign",
        "campaign__client",
        "transactional_message",
        "client",
    )
    if client is not None:
        queryset = queryset.filter(client=client)
    if valid_choice(event_type, EmailEventType):
        queryset = queryset.filter(event_type=event_type)
    return queryset.order_by("-created_at", "-id")
