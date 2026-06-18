from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from mailing.models import (
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    CampaignStatus,
    Contact,
    EmailEvent,
    EmailEventType,
    Subscription,
    SubscriptionStatus,
    normalize_tag_filter,
)
from mailing.queue_contracts import CAMPAIGN_EMAIL_CONTRACT, CONTRACT_VERSION, validate_campaign_email_message
from mailing.services.contacts import (
    has_invalid_email_validation,
    is_verified_for_marketing,
)
from mailing.sqs import enqueue_campaign_email


@dataclass(frozen=True)
class SnapshotResult:
    campaign_id: int
    created_count: int
    recipient_count: int
    skipped_count: int


@dataclass(frozen=True)
class RecipientPreviewRow:
    email: str
    status: str
    skip_reason: str


@dataclass(frozen=True)
class RecipientEstimate:
    total_candidates: int
    tag_filtered_count: int
    recipient_count: int
    skipped_count: int
    skip_reason_counts: dict[str, int]
    preview_rows: list[RecipientPreviewRow]


@dataclass(frozen=True)
class QueueCampaignResult:
    campaign_id: int
    queued: bool
    batch_count: int
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


def estimate_campaign_recipients(campaign, *, preview_limit=25):
    include_tags = normalize_tag_filter(campaign.include_tags)
    exclude_tags = normalize_tag_filter(campaign.exclude_tags)
    total_candidates = _base_candidate_contacts(campaign).count()
    candidate_contacts = _candidate_contacts(campaign, include_tags, exclude_tags)
    tag_filtered_count = total_candidates - candidate_contacts.count()
    recipient_count = 0
    skipped_count = 0
    skip_reason_counts = {reason: 0 for reason, _label in CampaignRecipientSkipReason.choices}
    preview_rows = []

    for contact in candidate_contacts:
        skip_reason = _skip_reason(contact, campaign)
        if skip_reason:
            skipped_count += 1
            skip_reason_counts[skip_reason] += 1
            status = CampaignRecipientStatus.SKIPPED
        else:
            recipient_count += 1
            status = CampaignRecipientStatus.PENDING

        if len(preview_rows) < preview_limit:
            preview_rows.append(
                RecipientPreviewRow(
                    email=contact.email,
                    status=status,
                    skip_reason=skip_reason,
                )
            )

    return RecipientEstimate(
        total_candidates=total_candidates,
        tag_filtered_count=tag_filtered_count,
        recipient_count=recipient_count,
        skipped_count=skipped_count,
        skip_reason_counts={reason: count for reason, count in skip_reason_counts.items() if count},
        preview_rows=preview_rows,
    )


def queue_campaign(campaign, *, batch_size=None):
    batch_size = batch_size or getattr(settings, "CAMPAIGN_EMAIL_BATCH_SIZE", 10)
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    with transaction.atomic():
        locked_campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
        if locked_campaign.status != CampaignStatus.DRAFT:
            return QueueCampaignResult(
                campaign_id=locked_campaign.id,
                queued=False,
                batch_count=0,
                recipient_count=locked_campaign.recipient_count,
                skipped_count=locked_campaign.skipped_count,
            )

        snapshot_campaign_recipients(locked_campaign)
        locked_campaign.refresh_from_db()
        recipient_ids = list(
            CampaignRecipient.objects.filter(
                campaign=locked_campaign,
                status=CampaignRecipientStatus.PENDING,
            )
            .order_by("id")
            .values_list("id", flat=True)
        )
        payloads = _campaign_email_payloads(locked_campaign.id, recipient_ids, batch_size)
        locked_campaign.status = CampaignStatus.QUEUED
        locked_campaign.save(update_fields=["status", "updated_at"])
        _append_queue_audit_events(locked_campaign, recipient_ids, len(payloads))

    for payload in payloads:
        enqueue_campaign_email(payload)

    return QueueCampaignResult(
        campaign_id=locked_campaign.id,
        queued=True,
        batch_count=len(payloads),
        recipient_count=locked_campaign.recipient_count,
        skipped_count=locked_campaign.skipped_count,
    )


def _candidate_contacts(campaign, include_tags, exclude_tags):
    queryset = _base_candidate_contacts(campaign)

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


def _base_candidate_contacts(campaign):
    contacts = (
        Subscription.objects.filter(audience=campaign.audience)
        .filter(Q(client=campaign.client) | Q(client__isnull=True))
        .select_related("contact")
        .order_by("contact__normalized_email", "contact_id")
        .distinct()
        .values_list("contact", flat=True)
    )
    return Contact.objects.filter(id__in=contacts).order_by("normalized_email", "id")


def _campaign_email_payloads(campaign_id, recipient_ids, batch_size):
    payloads = []
    for batch_index, offset in enumerate(range(0, len(recipient_ids), batch_size), start=1):
        batch_id = f"campaign-{campaign_id}-batch-{batch_index:04d}"
        payload = {
            "contract": CAMPAIGN_EMAIL_CONTRACT,
            "version": CONTRACT_VERSION,
            "campaign_id": campaign_id,
            "batch_id": batch_id,
            "campaign_recipient_ids": recipient_ids[offset : offset + batch_size],
            "idempotency_key": batch_id,
        }
        validate_campaign_email_message(payload)
        payloads.append(payload)
    return payloads


def _append_queue_audit_events(campaign, recipient_ids, batch_count):
    if not recipient_ids:
        return
    now = timezone.now()
    recipients = CampaignRecipient.objects.filter(id__in=recipient_ids).select_related("contact")
    EmailEvent.objects.bulk_create(
        [
            EmailEvent(
                campaign=campaign,
                campaign_recipient=recipient,
                contact=recipient.contact,
                client=campaign.client,
                audience=campaign.audience,
                event_type=EmailEventType.QUEUED,
                metadata={"batch_count": batch_count},
                created_at=now,
            )
            for recipient in recipients
        ]
    )


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
    if has_invalid_email_validation(contact):
        return CampaignRecipientSkipReason.INVALID_EMAIL
    audience_subscription = _subscription_for(contact, campaign, client=None)
    client_subscription = _subscription_for(contact, campaign, client=campaign.client)

    if not is_verified_for_marketing(
        contact,
        audience_subscription,
        client_subscription,
    ):
        return CampaignRecipientSkipReason.UNVERIFIED

    if audience_subscription and audience_subscription.status == SubscriptionStatus.UNSUBSCRIBED:
        return CampaignRecipientSkipReason.AUDIENCE_UNSUBSCRIBE

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
