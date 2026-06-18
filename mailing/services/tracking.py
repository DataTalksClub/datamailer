from urllib.parse import urlparse

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from mailing.models import Campaign, CampaignRecipient, CampaignRecipientStatus, EmailEvent, EmailEventType
from mailing.services.cmp_callbacks import emit_cmp_contact_event
from mailing.services.contacts import unsubscribe_contact
from mailing.services.tokens import get_recipient_by_tracking_token, get_recipient_by_unsubscribe_token

TRANSPARENT_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
    b"\x00\x02\x02D\x01\x00;"
)

UNSUBSCRIBE_SCOPES = {"client", "audience", "global"}


def is_allowed_click_destination(destination_url):
    if not destination_url:
        return False
    parsed = urlparse(destination_url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


@transaction.atomic
def record_open(raw_token):
    recipient = get_recipient_by_tracking_token(raw_token)
    if recipient is None:
        return None

    recipient = (
        CampaignRecipient.objects.select_for_update()
        .select_related(
            "campaign",
            "campaign__client",
            "campaign__audience",
            "contact",
        )
        .get(pk=recipient.pk)
    )
    now = timezone.now()
    update_fields = ["open_count", "updated_at"]
    recipient.open_count += 1
    if recipient.first_opened_at is None:
        recipient.first_opened_at = now
        update_fields.append("first_opened_at")
    recipient.save(update_fields=update_fields)
    _create_campaign_event(recipient, EmailEventType.OPEN)
    refresh_campaign_engagement_counts(recipient.campaign)
    return recipient


@transaction.atomic
def record_click(raw_token, destination_url):
    if not is_allowed_click_destination(destination_url):
        return None
    recipient = get_recipient_by_tracking_token(raw_token)
    if recipient is None:
        return None

    recipient = (
        CampaignRecipient.objects.select_for_update()
        .select_related(
            "campaign",
            "campaign__client",
            "campaign__audience",
            "contact",
        )
        .get(pk=recipient.pk)
    )
    now = timezone.now()
    update_fields = ["click_count", "updated_at"]
    recipient.click_count += 1
    if recipient.first_clicked_at is None:
        recipient.first_clicked_at = now
        update_fields.append("first_clicked_at")
    recipient.save(update_fields=update_fields)
    _create_campaign_event(recipient, EmailEventType.CLICK, url=destination_url)
    refresh_campaign_engagement_counts(recipient.campaign)
    return recipient


@transaction.atomic
def apply_unsubscribe(raw_token, scope):
    if scope not in UNSUBSCRIBE_SCOPES:
        return None
    recipient = get_recipient_by_unsubscribe_token(raw_token)
    if recipient is None:
        return None

    recipient = (
        CampaignRecipient.objects.select_for_update()
        .select_related(
            "campaign",
            "campaign__client",
            "campaign__audience",
            "contact",
        )
        .get(pk=recipient.pk)
    )
    now = timezone.now()
    campaign = recipient.campaign
    contact = recipient.contact

    if scope == "global":
        if contact.global_unsubscribed_at is None:
            contact.global_unsubscribed_at = now
            contact.save(update_fields=["global_unsubscribed_at", "updated_at"])
    elif scope == "audience":
        unsubscribe_contact(contact, campaign.audience, reason="public_unsubscribe", unsubscribed_at=now)
    else:
        unsubscribe_contact(
            contact, campaign.audience, campaign.client, reason="public_unsubscribe", unsubscribed_at=now
        )

    if recipient.status != CampaignRecipientStatus.UNSUBSCRIBED:
        recipient.status = CampaignRecipientStatus.UNSUBSCRIBED
        recipient.save(update_fields=["status", "updated_at"])

    event = _create_campaign_event(
        recipient,
        EmailEventType.UNSUBSCRIBE,
        metadata={"scope": scope},
    )
    emit_cmp_contact_event(event)
    refresh_campaign_engagement_counts(campaign)
    return recipient


def refresh_campaign_engagement_counts(campaign):
    aggregate = CampaignRecipient.objects.filter(campaign=campaign).aggregate(
        open_count=Sum("open_count"),
        click_count=Sum("click_count"),
    )
    counts = {
        "unique_open_count": CampaignRecipient.objects.filter(campaign=campaign, first_opened_at__isnull=False).count(),
        "open_count": aggregate["open_count"] or 0,
        "unique_click_count": CampaignRecipient.objects.filter(
            campaign=campaign, first_clicked_at__isnull=False
        ).count(),
        "click_count": aggregate["click_count"] or 0,
        "unsubscribe_count": CampaignRecipient.objects.filter(
            campaign=campaign,
            status=CampaignRecipientStatus.UNSUBSCRIBED,
        ).count(),
    }
    Campaign.objects.filter(pk=campaign.pk).update(**counts, updated_at=timezone.now())
    return counts


def _create_campaign_event(recipient, event_type, *, url="", metadata=None):
    return EmailEvent.objects.create(
        campaign=recipient.campaign,
        campaign_recipient=recipient,
        contact=recipient.contact,
        client=recipient.campaign.client,
        audience=recipient.campaign.audience,
        event_type=event_type,
        url=url,
        metadata=metadata or {},
    )
