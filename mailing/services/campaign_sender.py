from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mailing.aws import ses_client as default_ses_client
from mailing.models import Campaign, CampaignRecipient, CampaignRecipientStatus, EmailEvent, EmailEventType
from mailing.services.public_urls import click_redirect_url, open_pixel_url, unsubscribe_url
from mailing.services.tokens import (
    CampaignRecipientTokens,
    ensure_campaign_recipient_tokens,
    generate_raw_token,
    token_hash,
)
from mailing.ses import send_email

TERMINAL_STATUSES = {
    CampaignRecipientStatus.SENT,
    CampaignRecipientStatus.SKIPPED,
    CampaignRecipientStatus.FAILED,
    CampaignRecipientStatus.BOUNCED,
    CampaignRecipientStatus.COMPLAINED,
    CampaignRecipientStatus.UNSUBSCRIBED,
}

TRANSIENT_SES_ERROR_CODES = {
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
    "ServiceUnavailableException",
    "InternalError",
    "InternalFailure",
}


class CampaignSenderError(Exception):
    pass


class RetryableCampaignSendError(CampaignSenderError):
    pass


@dataclass(frozen=True)
class CampaignSendResult:
    sent_count: int
    skipped_count: int
    failed_count: int


def send_campaign_batch(payload, *, ses_client=None):
    campaign_id = payload["campaign_id"]
    recipient_ids = payload["campaign_recipient_ids"]
    ses = ses_client or default_ses_client()
    result = {"sent_count": 0, "skipped_count": 0, "failed_count": 0}

    if not Campaign.objects.filter(pk=campaign_id).exists():
        raise CampaignSenderError(f"campaign {campaign_id} does not exist")

    recipients = CampaignRecipient.objects.filter(pk__in=recipient_ids).only("id", "campaign_id")
    found_recipient_ids = {recipient.id for recipient in recipients}
    missing_recipient_ids = sorted(set(recipient_ids) - found_recipient_ids)
    wrong_campaign_ids = sorted(recipient.id for recipient in recipients if recipient.campaign_id != campaign_id)
    if missing_recipient_ids or wrong_campaign_ids:
        raise CampaignSenderError(
            f"invalid campaign recipient ids: missing={missing_recipient_ids}, wrong_campaign={wrong_campaign_ids}"
        )

    for recipient_id in recipient_ids:
        try:
            outcome = _send_campaign_recipient(campaign_id, recipient_id, ses)
        except RetryableCampaignSendError as exc:
            _record_retryable_error(campaign_id, recipient_id, str(exc))
            refresh_campaign_send_counts(campaign_id)
            raise
        result[f"{outcome}_count"] += 1

    refresh_campaign_send_counts(campaign_id)
    return CampaignSendResult(**result)


@transaction.atomic
def _send_campaign_recipient(campaign_id, recipient_id, ses):
    recipient = (
        CampaignRecipient.objects.select_for_update()
        .select_related("campaign", "campaign__client", "campaign__audience", "contact")
        .get(pk=recipient_id, campaign_id=campaign_id)
    )

    if recipient.status in TERMINAL_STATUSES or recipient.ses_message_id:
        return "skipped"

    if recipient.status != CampaignRecipientStatus.PENDING:
        return "skipped"

    tokens = _send_tokens_for_pending_recipient(recipient)

    html_body = build_campaign_html_body(recipient.campaign.html_body, tokens.tracking_token, tokens.unsubscribe_token)
    text_body = build_campaign_text_body(recipient.campaign.text_body, tokens.unsubscribe_token)

    try:
        message_id = send_email(
            ses_client=ses,
            source=settings.DEFAULT_FROM_EMAIL,
            to_email=recipient.email,
            subject=recipient.campaign.subject,
            html_body=html_body,
            text_body=text_body,
        )
    except Exception as exc:
        if is_retryable_send_error(exc):
            raise RetryableCampaignSendError(str(exc)) from exc
        _mark_recipient_failed(recipient, str(exc))
        return "failed"

    now = timezone.now()
    recipient.status = CampaignRecipientStatus.SENT
    recipient.ses_message_id = message_id
    recipient.sent_at = now
    recipient.last_error = ""
    recipient.save(update_fields=["status", "ses_message_id", "sent_at", "last_error", "updated_at"])
    _create_campaign_event(recipient, EmailEventType.SENT, metadata={"ses_message_id": message_id})
    return "sent"


def build_campaign_html_body(html_body, tracking_token, unsubscribe_token):
    body = rewrite_html_links(html_body or "", tracking_token)
    pixel = f'<img src="{escape(open_pixel_url(tracking_token), quote=True)}" width="1" height="1" alt="" />'
    unsubscribe_href = escape(unsubscribe_url(unsubscribe_token), quote=True)
    footer = f'<p><a href="{unsubscribe_href}">Unsubscribe or manage preferences</a></p>'
    return f"{body}\n{footer}\n{pixel}"


def build_campaign_text_body(text_body, unsubscribe_token):
    url = unsubscribe_url(unsubscribe_token)
    body = (text_body or "").rstrip()
    if body:
        return f"{body}\n\nUnsubscribe or manage preferences: {url}"
    return f"Unsubscribe or manage preferences: {url}"


def rewrite_html_links(html_body, tracking_token):
    rewriter = _ClickTrackingHTMLRewriter(tracking_token)
    rewriter.feed(html_body)
    rewriter.close()
    return rewriter.output


def is_retryable_send_error(exc):
    if isinstance(exc, (TimeoutError, ConnectionError, BotoCoreError)):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return code in TRANSIENT_SES_ERROR_CODES or status_code >= 500
    return False


def refresh_campaign_send_counts(campaign_id):
    sent_count = CampaignRecipient.objects.filter(campaign_id=campaign_id, status=CampaignRecipientStatus.SENT).count()
    failed_count = CampaignRecipient.objects.filter(
        campaign_id=campaign_id,
        status=CampaignRecipientStatus.FAILED,
    ).count()
    updates = {"sent_count": sent_count, "updated_at": timezone.now()}
    if sent_count:
        pending_exists = CampaignRecipient.objects.filter(
            campaign_id=campaign_id,
            status=CampaignRecipientStatus.PENDING,
        ).exists()
        if not pending_exists:
            updates["sent_at"] = timezone.now()
    Campaign.objects.filter(pk=campaign_id).update(**updates)
    return {"sent_count": sent_count, "failed_count": failed_count}


def _mark_recipient_failed(recipient, error_message):
    recipient.status = CampaignRecipientStatus.FAILED
    recipient.last_error = error_message[:2000]
    recipient.save(update_fields=["status", "last_error", "updated_at"])
    _create_campaign_event(recipient, EmailEventType.FAILED, metadata={"error": recipient.last_error})


def _send_tokens_for_pending_recipient(recipient):
    tokens = ensure_campaign_recipient_tokens(recipient)
    if tokens.tracking_token and tokens.unsubscribe_token:
        return tokens

    # Raw tokens are intentionally not stored by #10. If a retry reaches a
    # pending, unsent recipient with only hashes, no delivered email can depend
    # on those URLs yet, so rotating the hashes preserves that security model
    # while making the transient SES failure retryable.
    tracking_token = generate_raw_token()
    unsubscribe_token = generate_raw_token()
    recipient.tracking_token_hash = token_hash(tracking_token)
    recipient.unsubscribe_token_hash = token_hash(unsubscribe_token)
    recipient.save(update_fields=["tracking_token_hash", "unsubscribe_token_hash", "updated_at"])
    return CampaignRecipientTokens(
        tracking_token=tracking_token,
        unsubscribe_token=unsubscribe_token,
    )


def _record_retryable_error(campaign_id, recipient_id, error_message):
    CampaignRecipient.objects.filter(
        pk=recipient_id,
        campaign_id=campaign_id,
        status=CampaignRecipientStatus.PENDING,
    ).update(last_error=error_message[:2000], updated_at=timezone.now())


def _create_campaign_event(recipient, event_type, *, metadata=None):
    return EmailEvent.objects.create(
        campaign=recipient.campaign,
        campaign_recipient=recipient,
        contact=recipient.contact,
        client=recipient.campaign.client,
        audience=recipient.campaign.audience,
        event_type=event_type,
        metadata=metadata or {},
    )


class _ClickTrackingHTMLRewriter(HTMLParser):
    def __init__(self, tracking_token):
        super().__init__(convert_charrefs=False)
        self.tracking_token = tracking_token
        self.parts = []

    @property
    def output(self):
        return "".join(self.parts)

    def handle_starttag(self, tag, attrs):
        self.parts.append(self._format_tag(tag, attrs, closed=False))

    def handle_startendtag(self, tag, attrs):
        self.parts.append(self._format_tag(tag, attrs, closed=True))

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.parts.append(f"<?{data}>")

    def _format_tag(self, tag, attrs, *, closed):
        rewritten_attrs = []
        for name, value in attrs:
            if tag.lower() == "a" and name.lower() == "href" and _is_trackable_url(value):
                value = click_redirect_url(self.tracking_token, value)
            rewritten_attrs.append((name, value))

        attr_text = "".join(_format_attr(name, value) for name, value in rewritten_attrs)
        suffix = " />" if closed else ">"
        return f"<{tag}{attr_text}{suffix}"


def _format_attr(name, value):
    if value is None:
        return f" {name}"
    return f' {name}="{escape(value, quote=True)}"'


def _is_trackable_url(value):
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
