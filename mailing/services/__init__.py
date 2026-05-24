from mailing.services.campaigns import SnapshotResult, snapshot_campaign_recipients
from mailing.services.contacts import (
    assign_tag,
    get_audience_client_for_slugs,
    get_contact_suppression_state,
    get_subscription_for_slugs,
    is_marketing_email_allowed,
    is_transactional_email_allowed,
    normalize_email,
    subscribe_contact,
    unsubscribe_contact,
    upsert_contact,
)

__all__ = [
    "SnapshotResult",
    "assign_tag",
    "get_audience_client_for_slugs",
    "get_contact_suppression_state",
    "get_subscription_for_slugs",
    "is_marketing_email_allowed",
    "is_transactional_email_allowed",
    "normalize_email",
    "subscribe_contact",
    "snapshot_campaign_recipients",
    "unsubscribe_contact",
    "upsert_contact",
]
