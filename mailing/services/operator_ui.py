from __future__ import annotations

import json
from dataclasses import dataclass

from django.db.models import Count, Q, QuerySet

from mailing.models import (
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Contact,
    EmailEvent,
)
from mailing.services.contacts import normalize_email

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


@dataclass(frozen=True)
class Stat:
    key: str
    label: str
    value: int
    rate: str = ""


def rate(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return ""
    return f"{(numerator / denominator) * 100:.1f}%"


def campaign_stats(campaign: Campaign) -> list[Stat]:
    failed_count = CampaignRecipient.objects.filter(
        campaign=campaign,
        status=CampaignRecipientStatus.FAILED,
    ).count()
    recipient_count = campaign.recipient_count
    sent_count = campaign.sent_count

    return [
        Stat("recipients", "Recipients", recipient_count),
        Stat("sent", "Sent", sent_count, rate(sent_count, recipient_count)),
        Stat("skipped", "Skipped", campaign.skipped_count, rate(campaign.skipped_count, recipient_count)),
        Stat("delivered", "Delivered", campaign.delivered_count, rate(campaign.delivered_count, sent_count)),
        Stat("unique_opens", "Unique opens", campaign.unique_open_count, rate(campaign.unique_open_count, sent_count)),
        Stat("total_opens", "Total opens", campaign.open_count),
        Stat("unique_clicks", "Unique clicks", campaign.unique_click_count, rate(campaign.unique_click_count, sent_count)),
        Stat("total_clicks", "Total clicks", campaign.click_count),
        Stat("unsubscribes", "Unsubscribes", campaign.unsubscribe_count, rate(campaign.unsubscribe_count, sent_count)),
        Stat("bounces", "Bounces", campaign.bounce_count, rate(campaign.bounce_count, sent_count)),
        Stat("complaints", "Complaints", campaign.complaint_count, rate(campaign.complaint_count, sent_count)),
        Stat("failures", "Failures", failed_count, rate(failed_count, recipient_count)),
    ]


def campaign_queryset() -> QuerySet[Campaign]:
    return Campaign.objects.select_related("client", "audience", "audience__organization").order_by("-created_at", "-id")


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
    normalized = normalize_email(query)
    if not normalized:
        return Contact.objects.none()
    return (
        Contact.objects.filter(Q(normalized_email__icontains=normalized) | Q(email__icontains=query.strip()))
        .annotate(
            subscription_count=Count("subscriptions", distinct=True),
            campaign_recipient_count=Count("campaign_recipients", distinct=True),
            transactional_message_count=Count("transactional_messages", distinct=True),
        )
        .order_by("normalized_email", "id")
    )


def contact_detail_queryset() -> QuerySet[Contact]:
    return Contact.objects.prefetch_related(
        "subscriptions__audience__organization",
        "subscriptions__client",
        "contact_tags__tag__audience",
    )


def contact_campaign_history(contact: Contact) -> QuerySet[CampaignRecipient]:
    return (
        CampaignRecipient.objects.filter(contact=contact)
        .select_related("campaign", "campaign__client", "campaign__audience")
        .order_by("-created_at", "-id")
    )


def contact_transactional_history(contact: Contact):
    return (
        contact.transactional_messages.select_related("client")
        .order_by("-created_at", "-id")
        .only(
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
            "last_error",
        )
    )


def contact_event_timeline(contact: Contact) -> QuerySet[EmailEvent]:
    return (
        EmailEvent.objects.filter(contact=contact)
        .select_related(
            "campaign",
            "campaign__client",
            "campaign__audience",
            "campaign_recipient",
            "transactional_message",
            "client",
            "audience",
        )
        .order_by("-created_at", "-id")
    )


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
