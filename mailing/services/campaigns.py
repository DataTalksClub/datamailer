from dataclasses import dataclass

from django.db import transaction
from django.db.models import Count, Q, Sum

from mailing.models import (
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    Contact,
    Subscription,
    SubscriptionStatus,
    normalize_tag_filter,
)


@dataclass(frozen=True)
class SnapshotResult:
    campaign_id: int
    created_count: int
    recipient_count: int
    skipped_count: int


@transaction.atomic
def snapshot_campaign_recipients(campaign):
    campaign = (
        Campaign.objects.select_for_update()
        .select_related("audience", "client")
        .get(pk=campaign.pk)
    )
    include_tags = normalize_tag_filter(campaign.include_tags)
    exclude_tags = normalize_tag_filter(campaign.exclude_tags)
    candidate_contacts = _candidate_contacts(campaign, include_tags, exclude_tags)

    created_count = 0
    for contact in candidate_contacts:
        _, created = CampaignRecipient.objects.get_or_create(
            campaign=campaign,
            contact=contact,
            defaults={
                "email": contact.email,
                "status": _recipient_status(contact, campaign),
                "skip_reason": _skip_reason(contact, campaign),
            },
        )
        if created:
            created_count += 1

    counts = _refresh_campaign_counts(campaign)
    return SnapshotResult(
        campaign_id=campaign.id,
        created_count=created_count,
        recipient_count=counts["recipient_count"],
        skipped_count=counts["skipped_count"],
    )


def _candidate_contacts(campaign, include_tags, exclude_tags):
    contacts = (
        Subscription.objects.filter(audience=campaign.audience)
        .filter(Q(client=campaign.client) | Q(client__isnull=True))
        .select_related("contact")
        .order_by("contact__normalized_email", "contact_id")
        .distinct()
        .values_list("contact", flat=True)
    )

    queryset = Contact.objects.filter(id__in=contacts).order_by("normalized_email", "id")

    if include_tags:
        queryset = queryset.filter(contact_tags__tag__audience=campaign.audience, contact_tags__tag__slug__in=include_tags)
        queryset = queryset.annotate(
            matched_include_tag_count=Count(
                "contact_tags__tag__slug",
                filter=Q(contact_tags__tag__audience=campaign.audience, contact_tags__tag__slug__in=include_tags),
                distinct=True,
            )
        ).filter(matched_include_tag_count=len(include_tags))

    if exclude_tags:
        queryset = queryset.exclude(
            contact_tags__tag__audience=campaign.audience,
            contact_tags__tag__slug__in=exclude_tags,
        )

    return queryset


def _recipient_status(contact, campaign):
    if _skip_reason(contact, campaign):
        return CampaignRecipientStatus.SKIPPED
    return CampaignRecipientStatus.PENDING


def _skip_reason(contact, campaign):
    if contact.hard_bounced_at is not None:
        return CampaignRecipientSkipReason.HARD_BOUNCE
    if contact.complained_at is not None:
        return CampaignRecipientSkipReason.COMPLAINT
    if contact.global_unsubscribed_at is not None:
        return CampaignRecipientSkipReason.GLOBAL_UNSUBSCRIBE
    if contact.verified_at is None:
        return CampaignRecipientSkipReason.UNVERIFIED

    audience_subscription = _subscription_for(contact, campaign, client=None)
    if audience_subscription and audience_subscription.status == SubscriptionStatus.UNSUBSCRIBED:
        return CampaignRecipientSkipReason.AUDIENCE_UNSUBSCRIBE

    client_subscription = _subscription_for(contact, campaign, client=campaign.client)
    if client_subscription and client_subscription.status == SubscriptionStatus.UNSUBSCRIBED:
        return CampaignRecipientSkipReason.CLIENT_UNSUBSCRIBE
    if not client_subscription or client_subscription.status != SubscriptionStatus.SUBSCRIBED:
        return CampaignRecipientSkipReason.SUPPRESSED

    return ""


def _subscription_for(contact, campaign, client):
    return Subscription.objects.filter(
        contact=contact,
        audience=campaign.audience,
        client=client,
    ).first()


def _refresh_campaign_counts(campaign):
    recipient_count = CampaignRecipient.objects.filter(campaign=campaign).exclude(
        status=CampaignRecipientStatus.SKIPPED
    ).count()
    skipped_count = CampaignRecipient.objects.filter(
        campaign=campaign,
        status=CampaignRecipientStatus.SKIPPED,
    ).count()
    sent_count = CampaignRecipient.objects.filter(campaign=campaign, status=CampaignRecipientStatus.SENT).count()
    delivered_count = CampaignRecipient.objects.filter(campaign=campaign, delivered_at__isnull=False).count()
    unique_open_count = CampaignRecipient.objects.filter(campaign=campaign, first_opened_at__isnull=False).count()
    unique_click_count = CampaignRecipient.objects.filter(campaign=campaign, first_clicked_at__isnull=False).count()

    aggregate = CampaignRecipient.objects.filter(campaign=campaign).aggregate(
        open_count=Sum("open_count"),
        click_count=Sum("click_count"),
    )

    campaign.recipient_count = recipient_count
    campaign.skipped_count = skipped_count
    campaign.sent_count = sent_count
    campaign.delivered_count = delivered_count
    campaign.unique_open_count = unique_open_count
    campaign.unique_click_count = unique_click_count
    campaign.open_count = aggregate["open_count"] or 0
    campaign.click_count = aggregate["click_count"] or 0
    campaign.save(
        update_fields=[
            "recipient_count",
            "skipped_count",
            "sent_count",
            "delivered_count",
            "unique_open_count",
            "unique_click_count",
            "open_count",
            "click_count",
            "include_tags",
            "exclude_tags",
            "updated_at",
        ]
    )
    return {"recipient_count": recipient_count, "skipped_count": skipped_count}
