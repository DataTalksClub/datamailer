import base64
import json
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.request import urlopen

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.hashes import SHA1, SHA256
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mailing.models import (
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Contact,
    EmailEvent,
    EmailEventType,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.queue_contracts import CONTRACT_VERSION, SES_WEBHOOKS_CONTRACT, validate_ses_webhook_message
from mailing.services.cmp_callbacks import emit_cmp_contact_event
from mailing.sqs import enqueue_ses_webhook

SNS_NOTIFICATION = "Notification"
SNS_SUBSCRIPTION_CONFIRMATION = "SubscriptionConfirmation"
SUPPORTED_SNS_TYPES = {SNS_NOTIFICATION, SNS_SUBSCRIPTION_CONFIRMATION}
SNS_MOCK_SIGNATURE = "datamailer-local-sns-signature"

SES_EVENT_TYPES = {
    "Send": "send",
    "Reject": "reject",
    "Delivery": "delivery",
    "Bounce": "bounce",
    "Complaint": "complaint",
    "Open": "open",
    "Click": "click",
}

EVENT_TYPE_BY_NOTIFICATION = {
    "send": EmailEventType.SENT,
    "reject": EmailEventType.FAILED,
    "delivery": EmailEventType.DELIVERED,
    "bounce": EmailEventType.BOUNCE,
    "complaint": EmailEventType.COMPLAINT,
    "open": EmailEventType.OPEN,
    "click": EmailEventType.CLICK,
}


class SesWebhookError(ValueError):
    status_code = 400


class SnsSignatureError(SesWebhookError):
    status_code = 403


@dataclass(frozen=True)
class IngressResult:
    message_type: str
    enqueued: bool = False
    confirmed: bool = False


def ingest_sns_webhook(payload):
    if not isinstance(payload, dict):
        raise SesWebhookError("SNS payload must be a JSON object")

    message_type = payload.get("Type")
    if message_type not in SUPPORTED_SNS_TYPES:
        raise SesWebhookError("unsupported SNS message type")

    validate_sns_signature(payload)

    if message_type == SNS_SUBSCRIPTION_CONFIRMATION:
        confirmed = confirm_subscription(payload)
        return IngressResult(message_type=message_type, confirmed=confirmed)

    queue_payload = normalize_ses_notification(payload)
    enqueue_ses_webhook(queue_payload)
    return IngressResult(message_type=message_type, enqueued=True)


def validate_sns_signature(payload):
    mode = getattr(settings, "SES_WEBHOOKS_SIGNATURE_MODE", "strict")
    if mode == "disabled":
        return True
    if mode == "mock":
        if payload.get("Signature") == SNS_MOCK_SIGNATURE:
            return True
        raise SnsSignatureError("invalid mock SNS signature")
    if mode != "strict":
        raise SnsSignatureError("unsupported SNS signature mode")

    cert_url = payload.get("SigningCertURL", "")
    signature = payload.get("Signature", "")
    version = payload.get("SignatureVersion", "")
    if not cert_url or not signature or version not in {"1", "2"}:
        raise SnsSignatureError("missing SNS signature fields")

    cert = x509.load_pem_x509_certificate(fetch_signing_certificate(cert_url))
    algorithm = SHA256() if version == "2" else SHA1()
    try:
        cert.public_key().verify(
            base64.b64decode(signature),
            canonical_sns_message(payload).encode("utf-8"),
            padding.PKCS1v15(),
            algorithm,
        )
    except Exception as exc:
        raise SnsSignatureError("invalid SNS signature") from exc
    return True


def fetch_signing_certificate(cert_url):
    parsed = urlparse(cert_url)
    if parsed.scheme != "https" or not _is_allowed_sns_cert_host(parsed.netloc):
        raise SnsSignatureError("invalid SNS signing certificate URL")
    with urlopen(cert_url, timeout=5) as response:
        return response.read()


def canonical_sns_message(payload):
    fields_by_type = {
        SNS_NOTIFICATION: ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"],
        SNS_SUBSCRIPTION_CONFIRMATION: [
            "Message",
            "MessageId",
            "SubscribeURL",
            "Timestamp",
            "Token",
            "TopicArn",
            "Type",
        ],
    }
    parts = []
    for field in fields_by_type.get(payload.get("Type"), []):
        if field in payload:
            parts.append(f"{field}\n{payload[field]}\n")
    return "".join(parts)


def confirm_subscription(payload):
    if not getattr(settings, "SES_WEBHOOKS_ALLOW_SUBSCRIPTION_CONFIRMATION", False):
        return False
    subscribe_url = payload.get("SubscribeURL", "")
    if not subscribe_url:
        raise SesWebhookError("missing SubscribeURL")
    with urlopen(subscribe_url, timeout=5):
        return True


def normalize_ses_notification(sns_payload, *, received_at=None):
    try:
        ses_payload = json.loads(sns_payload["Message"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SesWebhookError("invalid SES notification message") from exc
    if not isinstance(ses_payload, dict):
        raise SesWebhookError("SES notification message must be an object")

    event_name = ses_payload.get("eventType") or ses_payload.get("notificationType")
    notification_type = SES_EVENT_TYPES.get(event_name)
    if notification_type is None:
        raise SesWebhookError("unsupported SES notification type")

    mail = ses_payload.get("mail") or {}
    ses_message_id = mail.get("messageId") or ses_payload.get("mailMessageId") or ""
    metadata = build_ses_metadata(ses_payload, sns_payload)
    payload = {
        "contract": SES_WEBHOOKS_CONTRACT,
        "version": CONTRACT_VERSION,
        "provider": "ses",
        "provider_event_id": sns_payload["MessageId"],
        "notification_type": notification_type,
        "received_at": (received_at or timezone.now()).isoformat(),
        "metadata": metadata,
    }
    if ses_message_id:
        payload["ses_message_id"] = ses_message_id
        payload["mail_message_id"] = ses_message_id
    return validate_ses_webhook_message(payload)


def normalize_ses_webhook_worker_payload(payload, *, received_at=None):
    if not isinstance(payload, dict):
        raise SesWebhookError("SES webhook worker payload must be a JSON object")

    if payload.get("contract") == SES_WEBHOOKS_CONTRACT:
        return validate_ses_webhook_message(payload)

    if payload.get("Type") == SNS_NOTIFICATION:
        return normalize_ses_notification(payload, received_at=received_at)

    raise SesWebhookError("unsupported SES webhook worker payload")


def build_ses_metadata(ses_payload, sns_payload):
    notification_type = SES_EVENT_TYPES[ses_payload.get("eventType") or ses_payload.get("notificationType")]
    detail = ses_payload.get(notification_type) or {}
    mail = ses_payload.get("mail") or {}
    metadata = {
        "sns_message_id": sns_payload.get("MessageId"),
        "sns_topic_arn": sns_payload.get("TopicArn"),
        "mail_timestamp": mail.get("timestamp"),
        "mail_source": mail.get("source"),
    }
    if notification_type == "bounce":
        metadata |= {
            "bounce_type": detail.get("bounceType"),
            "bounce_sub_type": detail.get("bounceSubType"),
        }
    elif notification_type == "complaint":
        metadata["complaint_feedback_type"] = detail.get("complaintFeedbackType")
    elif notification_type == "reject":
        metadata["reason"] = detail.get("reason")
    elif notification_type == "click":
        metadata["url"] = detail.get("link")
    return {key: value for key, value in metadata.items() if value not in (None, "")}


def process_ses_webhook(payload):
    validate_ses_webhook_message(payload)
    provider_event_id = payload["provider_event_id"]
    if EmailEvent.objects.filter(provider_event_id=provider_event_id).exists():
        return None

    with transaction.atomic():
        if EmailEvent.objects.select_for_update().filter(provider_event_id=provider_event_id).exists():
            return None

        source = correlate_ses_message(payload.get("ses_message_id", ""))
        event = append_provider_event(payload, source)
        if event is None:
            return None
        if source is not None:
            apply_correlated_updates(payload, source, event)
        return event


def correlate_ses_message(ses_message_id):
    if not ses_message_id:
        return None

    campaign_recipients = (
        CampaignRecipient.objects.select_for_update()
        .select_related("campaign", "campaign__client", "campaign__audience", "contact")
        .filter(ses_message_id=ses_message_id)
    )
    transactional_messages = (
        TransactionalMessage.objects.select_for_update()
        .select_related("client", "contact")
        .filter(ses_message_id=ses_message_id)
    )
    if campaign_recipients.count() == 1 and not transactional_messages.exists():
        return campaign_recipients.first()
    if transactional_messages.count() == 1 and not campaign_recipients.exists():
        return transactional_messages.first()
    return None


def append_provider_event(payload, source):
    notification_type = payload["notification_type"]
    metadata = webhook_event_metadata(payload)
    url = metadata.get("url", "")
    event_type = EVENT_TYPE_BY_NOTIFICATION[notification_type]
    kwargs = {
        "event_type": event_type,
        "provider_event_id": payload["provider_event_id"],
        "url": url if notification_type == "click" else "",
        "metadata": metadata,
    }
    if isinstance(source, CampaignRecipient):
        kwargs |= {
            "campaign": source.campaign,
            "campaign_recipient": source,
            "contact": source.contact,
            "client": source.campaign.client,
            "audience": source.campaign.audience,
        }
    elif isinstance(source, TransactionalMessage):
        kwargs |= {
            "transactional_message": source,
            "contact": source.contact,
            "client": source.client,
        }
    return EmailEvent.objects.create(**kwargs)


def apply_correlated_updates(payload, source, event):
    notification_type = payload["notification_type"]
    occurred_at = event_timestamp(payload)
    metadata = payload.get("metadata") or {}
    if isinstance(source, CampaignRecipient):
        apply_campaign_recipient_update(source, notification_type, occurred_at, metadata)
        refresh_campaign_provider_counts(source.campaign_id)
    elif isinstance(source, TransactionalMessage):
        apply_transactional_message_update(source, notification_type, occurred_at, metadata)

    if notification_type in {"delivery", "open", "click", "complaint"} or (
        notification_type == "bounce" and is_hard_bounce(metadata)
    ):
        emit_cmp_contact_event(event)


def apply_campaign_recipient_update(recipient, notification_type, occurred_at, metadata):
    update_fields = ["updated_at"]
    if notification_type == "delivery" and recipient.delivered_at is None:
        recipient.delivered_at = occurred_at
        update_fields.append("delivered_at")
    elif notification_type == "bounce":
        if is_hard_bounce(metadata):
            recipient.status = CampaignRecipientStatus.BOUNCED
            update_fields.append("status")
            mark_hard_bounced(recipient.contact, occurred_at)
        else:
            recipient.last_error = "soft_bounce"
            update_fields.append("last_error")
    elif notification_type == "complaint":
        recipient.status = CampaignRecipientStatus.COMPLAINED
        update_fields.append("status")
        mark_complained(recipient.contact, occurred_at)
    elif notification_type == "open":
        recipient.open_count += 1
        update_fields.append("open_count")
        if recipient.first_opened_at is None:
            recipient.first_opened_at = occurred_at
            update_fields.append("first_opened_at")
    elif notification_type == "click":
        recipient.click_count += 1
        update_fields.append("click_count")
        if recipient.first_clicked_at is None:
            recipient.first_clicked_at = occurred_at
            update_fields.append("first_clicked_at")
    elif notification_type == "reject":
        recipient.status = CampaignRecipientStatus.FAILED
        recipient.last_error = metadata.get("reason", "ses_reject")[:2000]
        update_fields += ["status", "last_error"]

    recipient.save(update_fields=sorted(set(update_fields)))


def apply_transactional_message_update(message, notification_type, occurred_at, metadata):
    update_fields = ["updated_at"]
    if notification_type == "delivery" and message.delivered_at is None:
        message.delivered_at = occurred_at
        update_fields.append("delivered_at")
    elif notification_type == "bounce":
        if is_hard_bounce(metadata):
            message.status = TransactionalMessageStatus.BOUNCED
            update_fields.append("status")
            mark_hard_bounced(message.contact, occurred_at)
        else:
            message.last_error = "soft_bounce"
            update_fields.append("last_error")
    elif notification_type == "complaint":
        message.status = TransactionalMessageStatus.COMPLAINED
        update_fields.append("status")
        mark_complained(message.contact, occurred_at)
    elif notification_type == "open":
        message.open_count += 1
        update_fields.append("open_count")
        if message.first_opened_at is None:
            message.first_opened_at = occurred_at
            update_fields.append("first_opened_at")
    elif notification_type == "click":
        message.click_count += 1
        update_fields.append("click_count")
        if message.first_clicked_at is None:
            message.first_clicked_at = occurred_at
            update_fields.append("first_clicked_at")
    elif notification_type == "reject":
        message.status = TransactionalMessageStatus.FAILED
        message.last_error = metadata.get("reason", "ses_reject")[:2000]
        update_fields += ["status", "last_error"]

    message.save(update_fields=sorted(set(update_fields)))


def refresh_campaign_provider_counts(campaign_id):
    campaign = Campaign.objects.get(pk=campaign_id)
    recipients = CampaignRecipient.objects.filter(campaign_id=campaign_id)
    campaign.delivered_count = recipients.filter(delivered_at__isnull=False).count()
    campaign.bounce_count = recipients.filter(status=CampaignRecipientStatus.BOUNCED).count()
    campaign.complaint_count = recipients.filter(status=CampaignRecipientStatus.COMPLAINED).count()
    campaign.unique_open_count = recipients.filter(first_opened_at__isnull=False).count()
    campaign.unique_click_count = recipients.filter(first_clicked_at__isnull=False).count()
    campaign.open_count = sum(recipients.values_list("open_count", flat=True))
    campaign.click_count = sum(recipients.values_list("click_count", flat=True))
    campaign.save(
        update_fields=[
            "delivered_count",
            "bounce_count",
            "complaint_count",
            "unique_open_count",
            "unique_click_count",
            "open_count",
            "click_count",
            "updated_at",
        ]
    )


def mark_hard_bounced(contact, occurred_at):
    Contact.objects.filter(pk=contact.pk, hard_bounced_at__isnull=True).update(
        hard_bounced_at=occurred_at,
        updated_at=timezone.now(),
    )


def mark_complained(contact, occurred_at):
    Contact.objects.filter(pk=contact.pk, complained_at__isnull=True).update(
        complained_at=occurred_at,
        updated_at=timezone.now(),
    )


def is_hard_bounce(metadata):
    bounce_type = (metadata.get("bounce_type") or "").casefold()
    bounce_sub_type = (metadata.get("bounce_sub_type") or "").casefold()
    return bounce_type == "permanent" or bounce_sub_type in {"general", "suppressed", "onaccountsuppressionlist"}


def event_timestamp(payload):
    metadata = payload.get("metadata") or {}
    for key in ("event_timestamp", "mail_timestamp"):
        value = metadata.get(key)
        parsed = parse_datetime(value) if isinstance(value, str) else None
        if parsed is not None:
            return parsed if parsed.tzinfo else timezone.make_aware(parsed)
    parsed = parse_datetime(payload.get("received_at", ""))
    if parsed is not None:
        return parsed if parsed.tzinfo else timezone.make_aware(parsed)
    return timezone.now()


def webhook_event_metadata(payload):
    metadata = dict(payload.get("metadata") or {})
    metadata.update(
        {
            "provider": payload["provider"],
            "provider_event_id": payload["provider_event_id"],
            "notification_type": payload["notification_type"],
            "ses_message_id": payload.get("ses_message_id", ""),
            "received_at": payload["received_at"],
        }
    )
    return metadata


def _is_allowed_sns_cert_host(host):
    return host.startswith("sns.") and (host.endswith(".amazonaws.com") or host.endswith(".amazonaws.com.cn"))
