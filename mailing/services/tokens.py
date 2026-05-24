import hashlib
import secrets
from dataclasses import dataclass

from django.db import transaction

from mailing.models import CampaignRecipient

TOKEN_BYTES = 32


@dataclass(frozen=True)
class CampaignRecipientTokens:
    tracking_token: str | None
    unsubscribe_token: str | None


def token_hash(raw_token):
    try:
        token_bytes = raw_token.encode("ascii")
    except UnicodeEncodeError:
        return None
    return hashlib.sha256(token_bytes).hexdigest()


def generate_raw_token():
    return secrets.token_urlsafe(TOKEN_BYTES)


@transaction.atomic
def ensure_campaign_recipient_tokens(recipient):
    recipient = CampaignRecipient.objects.select_for_update().get(pk=recipient.pk)
    tracking_token = None
    unsubscribe_token = None

    updates = []
    if not recipient.tracking_token_hash:
        tracking_token = generate_raw_token()
        tracking_hash = token_hash(tracking_token)
        recipient.tracking_token_hash = tracking_hash
        updates.append("tracking_token_hash")
    if not recipient.unsubscribe_token_hash:
        unsubscribe_token = generate_raw_token()
        unsubscribe_hash = token_hash(unsubscribe_token)
        recipient.unsubscribe_token_hash = unsubscribe_hash
        updates.append("unsubscribe_token_hash")
    if updates:
        recipient.save(update_fields=[*updates, "updated_at"])

    return CampaignRecipientTokens(
        tracking_token=tracking_token,
        unsubscribe_token=unsubscribe_token,
    )


def get_recipient_by_tracking_token(raw_token):
    return _get_recipient_by_token_hash(raw_token, "tracking_token_hash")


def get_recipient_by_unsubscribe_token(raw_token):
    return _get_recipient_by_token_hash(raw_token, "unsubscribe_token_hash")


def _get_recipient_by_token_hash(raw_token, hash_field):
    if not raw_token:
        return None
    hashed_token = token_hash(raw_token)
    if hashed_token is None:
        return None
    return (
        CampaignRecipient.objects.select_related(
            "campaign",
            "campaign__client",
            "campaign__audience",
            "contact",
        )
        .filter(**{hash_field: hashed_token})
        .first()
    )
