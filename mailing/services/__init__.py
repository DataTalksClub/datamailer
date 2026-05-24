from mailing.services.campaigns import (
    QueueCampaignResult,
    RecipientEstimate,
    SnapshotResult,
    estimate_campaign_recipients,
    queue_campaign,
    snapshot_campaign_recipients,
)
from mailing.services.contacts import (
    assign_tag,
    get_audience_client_for_slugs,
    get_contact_suppression_state,
    get_subscription_for_slugs,
    has_invalid_email_validation,
    is_marketing_email_allowed,
    is_transactional_email_allowed,
    normalize_email,
    subscribe_contact,
    unsubscribe_contact,
    upsert_contact,
)
from mailing.services.public_urls import (
    campaign_recipient_public_urls,
    click_redirect_url,
    open_pixel_url,
    unsubscribe_url,
)
from mailing.services.tokens import ensure_campaign_recipient_tokens, token_hash
from mailing.services.tracking import apply_unsubscribe, record_click, record_open

__all__ = [
    "SnapshotResult",
    "QueueCampaignResult",
    "RecipientEstimate",
    "apply_unsubscribe",
    "assign_tag",
    "campaign_recipient_public_urls",
    "click_redirect_url",
    "ensure_campaign_recipient_tokens",
    "get_audience_client_for_slugs",
    "get_contact_suppression_state",
    "get_subscription_for_slugs",
    "has_invalid_email_validation",
    "estimate_campaign_recipients",
    "is_marketing_email_allowed",
    "is_transactional_email_allowed",
    "normalize_email",
    "open_pixel_url",
    "queue_campaign",
    "record_click",
    "record_open",
    "subscribe_contact",
    "snapshot_campaign_recipients",
    "token_hash",
    "unsubscribe_contact",
    "unsubscribe_url",
    "upsert_contact",
]
