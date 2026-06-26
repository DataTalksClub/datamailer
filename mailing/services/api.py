from dataclasses import dataclass
from email.utils import parseaddr

from django.core.exceptions import ValidationError
from django.core.validators import validate_email, validate_slug
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    CategoryPreference,
    Contact,
    ContactTag,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    EmailValidationStatus,
    Subscription,
    SubscriptionStatus,
    TransactionalMessage,
    normalize_tag_filter,
)
from mailing.services.campaign_sender import render_campaign_message, send_campaign_test_message
from mailing.services.campaigns import queue_campaign
from mailing.services.cmp_callbacks import emit_cmp_contact_event
from mailing.services.contacts import (
    assign_tag,
    is_marketing_email_allowed,
    is_transactional_email_allowed,
    is_verified_for_marketing,
    normalize_email,
    normalize_tag_slug,
    subscribe_contact,
    unsubscribe_contact,
    upsert_contact,
)


class ApiValidationError(Exception):
    def __init__(self, errors, *, status_code=400):
        self.errors = errors
        self.status_code = status_code
        super().__init__("api_validation_error")


@dataclass(frozen=True)
class ScopedRequest:
    email: str
    audience: Audience
    client: object


def template_payload(template):
    return {
        "key": template.key,
        "client": template.client.slug,
        "name": template.name,
        "description": template.description,
        "subject": template.subject,
        "html_body": template.html_body,
        "text_body": template.text_body,
        "required_context": template.required_context,
        "example_context": template.example_context,
        "default_sender_id": template.default_sender_id,
        "is_transactional": template.is_transactional,
        "is_active": template.is_active,
        "created_at": isoformat(template.created_at),
        "updated_at": isoformat(template.updated_at),
    }


def sender_policy_payload(client):
    return {
        "client": {
            "organization": client.organization.slug,
            "slug": client.slug,
            "name": client.name,
        },
        "default_sender_id": client.default_sender_id,
        "senders": client.sender_emails or [],
    }


def validate_sender_policy_payload(data):
    errors = {}

    senders = data.get("senders", data.get("sender_emails"))
    if not isinstance(senders, list) or not senders:
        errors["senders"] = "must_be_non_empty_list"
        senders = []

    normalized_senders = []
    seen_sender_ids = set()
    for index, sender in enumerate(senders):
        if not isinstance(sender, dict):
            errors[f"senders.{index}"] = "must_be_object"
            continue

        sender_id = sender.get("id")
        if not isinstance(sender_id, str) or not sender_id.strip():
            errors[f"senders.{index}.id"] = "required"
            continue
        sender_id = sender_id.strip()
        try:
            validate_slug(sender_id)
        except ValidationError:
            errors[f"senders.{index}.id"] = "invalid"
            continue
        if sender_id in seen_sender_ids:
            errors[f"senders.{index}.id"] = "duplicate"
            continue
        seen_sender_ids.add(sender_id)

        sender_email = sender.get("email")
        if not isinstance(sender_email, str) or not sender_email.strip():
            errors[f"senders.{index}.email"] = "required"
            continue
        sender_email = sender_email.strip()
        _, parsed_email = parseaddr(sender_email)
        email_to_validate = parsed_email or sender_email
        try:
            validate_email(email_to_validate)
        except ValidationError:
            errors[f"senders.{index}.email"] = "invalid"
            continue

        normalized_senders.append({"id": sender_id, "email": sender_email})

    default_sender_id = data.get("default_sender_id", "")
    if default_sender_id in (None, "") and normalized_senders:
        default_sender_id = normalized_senders[0]["id"]
    elif not isinstance(default_sender_id, str) or not default_sender_id.strip():
        errors["default_sender_id"] = "required"
    else:
        default_sender_id = default_sender_id.strip()
        try:
            validate_slug(default_sender_id)
        except ValidationError:
            errors["default_sender_id"] = "invalid"

    if (
        isinstance(default_sender_id, str)
        and default_sender_id
        and default_sender_id not in {sender["id"] for sender in normalized_senders}
    ):
        errors["default_sender_id"] = "not_configured"

    if errors:
        raise ApiValidationError(errors)

    return {
        "default_sender_id": default_sender_id,
        "sender_emails": normalized_senders,
    }


def get_client_sender_policy_for_client(authenticated_client):
    return sender_policy_payload(authenticated_client)


def campaign_payload(campaign):
    return {
        "external_key": campaign.external_key,
        "audience": campaign.audience.slug,
        "client": campaign.client.slug,
        "subject": campaign.subject,
        "preview_text": campaign.preview_text,
        "html_body": campaign.html_body,
        "text_body": campaign.text_body,
        "status": campaign.status,
        "scheduled_at": isoformat(campaign.scheduled_at),
        "sent_at": isoformat(campaign.sent_at),
        "include_tags": campaign.include_tags,
        "exclude_tags": campaign.exclude_tags,
        "recipient_count": campaign.recipient_count,
        "sent_count": campaign.sent_count,
        "skipped_count": campaign.skipped_count,
        "delivered_count": campaign.delivered_count,
        "unique_open_count": campaign.unique_open_count,
        "open_count": campaign.open_count,
        "unique_click_count": campaign.unique_click_count,
        "click_count": campaign.click_count,
        "unsubscribe_count": campaign.unsubscribe_count,
        "bounce_count": campaign.bounce_count,
        "complaint_count": campaign.complaint_count,
        "created_at": isoformat(campaign.created_at),
        "updated_at": isoformat(campaign.updated_at),
    }


def validate_campaign_external_key(value):
    if not isinstance(value, str) or not value.strip():
        raise ApiValidationError({"external_key": "required"})
    external_key = value.strip()
    if "/" in external_key or len(external_key) > 180:
        raise ApiValidationError({"external_key": "invalid"})
    return external_key


def validate_campaign_tag_filter(value, field):
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ApiValidationError({field: "must_be_list"})
    return normalize_tag_filter(value)


def validate_campaign_payload(data, authenticated_client):
    scope = validate_contact_scope(data, authenticated_client, require_email=False)
    errors = {}

    subject = data.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        errors["subject"] = "required"
        subject = ""
    elif len(subject.strip()) > 255:
        errors["subject"] = "too_long"

    preview_text = data.get("preview_text", "")
    if preview_text in (None, ""):
        preview_text = ""
    elif not isinstance(preview_text, str):
        errors["preview_text"] = "must_be_string"
    elif len(preview_text.strip()) > 255:
        errors["preview_text"] = "too_long"

    html_body = data.get("html_body", "")
    if html_body in (None, ""):
        html_body = ""
    elif not isinstance(html_body, str):
        errors["html_body"] = "must_be_string"

    text_body = data.get("text_body", "")
    if text_body in (None, ""):
        text_body = ""
    elif not isinstance(text_body, str):
        errors["text_body"] = "must_be_string"

    if not str(html_body).strip() and not str(text_body).strip():
        errors["body"] = "required"

    scheduled_at = data.get("scheduled_at")
    if scheduled_at in (None, ""):
        scheduled_at = None
    elif not isinstance(scheduled_at, str):
        errors["scheduled_at"] = "must_be_string"
    else:
        scheduled_at = parse_datetime(scheduled_at.strip())
        if scheduled_at is None:
            errors["scheduled_at"] = "invalid"
        elif timezone.is_naive(scheduled_at):
            scheduled_at = timezone.make_aware(scheduled_at, timezone.get_current_timezone())

    try:
        include_tags = validate_campaign_tag_filter(data.get("include_tags"), "include_tags")
    except ApiValidationError as exc:
        errors.update(exc.errors)
        include_tags = []
    try:
        exclude_tags = validate_campaign_tag_filter(data.get("exclude_tags"), "exclude_tags")
    except ApiValidationError as exc:
        errors.update(exc.errors)
        exclude_tags = []

    if errors:
        raise ApiValidationError(errors)

    return {
        "audience": scope.audience,
        "client": scope.client,
        "subject": subject.strip(),
        "preview_text": preview_text.strip(),
        "html_body": html_body,
        "text_body": text_body,
        "scheduled_at": scheduled_at,
        "include_tags": include_tags,
        "exclude_tags": exclude_tags,
    }


def get_campaign_for_client(external_key, data, authenticated_client):
    campaign = campaign_for_client(external_key, data, authenticated_client)
    return {"campaign": campaign_payload(campaign)}


def campaign_for_client(external_key, data, authenticated_client, *, for_update=False):
    external_key = validate_campaign_external_key(external_key)
    scope = validate_contact_scope(data, authenticated_client, require_email=False)
    queryset = Campaign.objects
    if for_update:
        queryset = queryset.select_for_update()
    campaign = queryset.filter(
        client=scope.client,
        audience=scope.audience,
        external_key=external_key,
    ).first()
    if campaign is None:
        raise ApiValidationError({"external_key": "not_found"}, status_code=404)
    return campaign


@transaction.atomic
def upsert_campaign_for_client(external_key, data, authenticated_client):
    external_key = validate_campaign_external_key(external_key)
    defaults = validate_campaign_payload(data, authenticated_client)
    existing = Campaign.objects.select_for_update().filter(
        client=defaults["client"],
        external_key=external_key,
    ).first()
    if existing is not None and existing.audience_id != defaults["audience"].id:
        raise ApiValidationError({"external_key": "audience_mismatch"}, status_code=409)
    if existing is not None and existing.status != CampaignStatus.DRAFT:
        raise ApiValidationError({"status": "not_editable"}, status_code=409)

    campaign, created = Campaign.objects.update_or_create(
        client=defaults["client"],
        external_key=external_key,
        defaults=defaults,
    )
    return {
        "campaign": campaign_payload(campaign),
        "created": created,
    }


def queue_campaign_for_client(external_key, data, authenticated_client):
    campaign = campaign_for_client(external_key, data, authenticated_client)
    if campaign.status != CampaignStatus.DRAFT:
        raise ApiValidationError({"status": "not_queueable"}, status_code=409)

    result = queue_campaign(campaign)
    campaign.refresh_from_db()
    return {
        "campaign": campaign_payload(campaign),
        "queued": result.queued,
        "batch_count": result.batch_count,
        "recipient_count": result.recipient_count,
        "skipped_count": result.skipped_count,
    }


@transaction.atomic
def cancel_campaign_for_client(external_key, data, authenticated_client):
    campaign = campaign_for_client(external_key, data, authenticated_client, for_update=True)
    if campaign.status == CampaignStatus.CANCELLED:
        return {"campaign": campaign_payload(campaign), "cancelled": False}
    if campaign.status == CampaignStatus.DRAFT:
        campaign.status = CampaignStatus.CANCELLED
        campaign.save(update_fields=["status", "updated_at"])
        return {"campaign": campaign_payload(campaign), "cancelled": True}
    has_sent_recipients = CampaignRecipient.objects.filter(
        campaign=campaign,
        status=CampaignRecipientStatus.SENT,
    ).exists()
    if campaign.status == CampaignStatus.QUEUED and campaign.sent_count == 0 and not has_sent_recipients:
        skipped = CampaignRecipient.objects.filter(
            campaign=campaign,
            status=CampaignRecipientStatus.PENDING,
        ).update(
            status=CampaignRecipientStatus.SKIPPED,
            last_error="campaign_cancelled",
            updated_at=timezone.now(),
        )
        campaign.status = CampaignStatus.CANCELLED
        campaign.skipped_count += skipped
        campaign.recipient_count = max(campaign.recipient_count - skipped, 0)
        campaign.save(update_fields=["status", "skipped_count", "recipient_count", "updated_at"])
        return {"campaign": campaign_payload(campaign), "cancelled": True, "skipped_count": skipped}

    raise ApiValidationError({"status": "not_cancellable"}, status_code=409)


def preview_campaign_for_client(external_key, data, authenticated_client):
    campaign = campaign_for_client(external_key, data, authenticated_client)
    return {
        "campaign": campaign_payload(campaign),
        "preview": render_campaign_message(campaign),
    }


def validate_test_recipient_emails(value):
    if not isinstance(value, list) or not value:
        raise ApiValidationError({"emails": "required"})
    if len(value) > 25:
        raise ApiValidationError({"emails": "too_many"})

    emails = []
    errors = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append({"index": index, "error": "required"})
            continue
        email = item.strip()
        try:
            validate_email(email)
        except ValidationError:
            errors.append({"index": index, "email": email, "error": "invalid"})
            continue
        emails.append(normalize_email(email))

    if errors:
        raise ApiValidationError({"emails": errors})
    return sorted(set(emails))


def test_send_campaign_for_client(external_key, data, authenticated_client):
    campaign = campaign_for_client(external_key, data, authenticated_client)
    emails = validate_test_recipient_emails(data.get("emails"))
    sent = []
    for email in emails:
        sent.append(
            {
                "email": email,
                "message_id": send_campaign_test_message(campaign, email),
            }
        )
    return {
        "campaign": campaign_payload(campaign),
        "sent_count": len(sent),
        "recipients": sent,
    }


@transaction.atomic
def update_client_sender_policy_for_client(data, authenticated_client):
    updates = validate_sender_policy_payload(data)
    authenticated_client.default_sender_id = updates["default_sender_id"]
    authenticated_client.sender_emails = updates["sender_emails"]
    authenticated_client.save(update_fields=["default_sender_id", "sender_emails", "updated_at"])
    return sender_policy_payload(authenticated_client)


def validate_transactional_template_payload(data, template_key):
    errors = {}

    try:
        validate_slug(template_key)
    except ValidationError:
        errors["template_key"] = "invalid"

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        errors["name"] = "required"

    subject = data.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        errors["subject"] = "required"

    fields = {
        "description": data.get("description", ""),
        "html_body": data.get("html_body", ""),
        "text_body": data.get("text_body", ""),
    }
    for field_name, value in fields.items():
        if not isinstance(value, str):
            errors[field_name] = "must_be_string"

    required_context = data.get("required_context", [])
    if required_context in (None, ""):
        required_context = []
    elif not isinstance(required_context, list):
        errors["required_context"] = "must_be_list"

    example_context = data.get("example_context", {})
    if example_context in (None, ""):
        example_context = {}
    elif not isinstance(example_context, dict):
        errors["example_context"] = "must_be_object"

    is_active = data.get("is_active", True)
    if not isinstance(is_active, bool):
        errors["is_active"] = "must_be_boolean"

    default_sender_id = data.get("default_sender_id", "")
    if default_sender_id in (None, ""):
        default_sender_id = ""
    elif not isinstance(default_sender_id, str) or not default_sender_id.strip():
        errors["default_sender_id"] = "must_be_non_empty_string"
    else:
        try:
            default_sender_id = default_sender_id.strip()
            validate_slug(default_sender_id)
        except ValidationError:
            errors["default_sender_id"] = "invalid"

    if errors:
        raise ApiValidationError(errors)

    return {
        "name": name.strip(),
        "subject": subject.strip(),
        "description": fields["description"].strip(),
        "html_body": fields["html_body"],
        "text_body": fields["text_body"],
        "required_context": required_context,
        "example_context": example_context,
        "default_sender_id": default_sender_id,
        "is_transactional": True,
        "is_active": is_active,
    }


def get_transactional_template_for_client(template_key, authenticated_client):
    template = EmailTemplate.objects.filter(
        client=authenticated_client,
        key=template_key,
        is_transactional=True,
    ).first()
    if template is None:
        raise ApiValidationError({"template_key": "not_found"}, status_code=404)
    return template_payload(template)


@transaction.atomic
def upsert_transactional_template_for_client(template_key, data, authenticated_client):
    defaults = validate_transactional_template_payload(data, template_key)
    configured_sender_ids = {
        sender.get("id")
        for sender in authenticated_client.sender_emails or []
        if isinstance(sender, dict)
    }
    if (
        defaults["default_sender_id"]
        and defaults["default_sender_id"] not in configured_sender_ids
    ):
        raise ApiValidationError({"default_sender_id": "not_configured"})
    template, created = EmailTemplate.objects.update_or_create(
        client=authenticated_client,
        key=template_key,
        defaults=defaults,
    )
    return {
        "template": template_payload(template),
        "created": created,
    }


def validate_contact_scope(data, authenticated_client, *, require_email=True, require_client=True):
    errors = {}

    email = data.get("email")
    if require_email:
        if not isinstance(email, str) or not email.strip():
            errors["email"] = "required"
        else:
            try:
                validate_email(email.strip())
            except ValidationError:
                errors["email"] = "invalid"

    audience_slug = data.get("audience")
    if not isinstance(audience_slug, str) or not audience_slug.strip():
        errors["audience"] = "required"

    client_slug = data.get("client")
    if require_client and (not isinstance(client_slug, str) or not client_slug.strip()):
        errors["client"] = "required"
    elif isinstance(client_slug, str) and client_slug.strip() != authenticated_client.slug:
        errors["client"] = "forbidden"

    audience = None
    if "audience" not in errors:
        audience = Audience.objects.filter(
            organization=authenticated_client.organization,
            slug=audience_slug.strip(),
        ).first()
        if audience is None:
            errors["audience"] = "not_found"

    if errors:
        status_code = 403 if "forbidden" in errors.values() else 400
        raise ApiValidationError(errors, status_code=status_code)

    scoped_email = email.strip() if isinstance(email, str) else ""
    return ScopedRequest(email=scoped_email, audience=audience, client=authenticated_client)


def validate_tags(value):
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ApiValidationError({"tags": "must_be_list"})

    tags = []
    for tag in value:
        if not isinstance(tag, str) or not tag.strip():
            raise ApiValidationError({"tags": "must_be_non_empty_strings"})
        tags.append(tag.strip())
    return tags


def validate_tag_slug(value):
    if not isinstance(value, str) or not value.strip():
        raise ApiValidationError({"tag": "required"})
    tag_slug = normalize_tag_slug(value)
    if not tag_slug:
        raise ApiValidationError({"tag": "invalid"})
    return tag_slug


def validate_status(value, *, default=SubscriptionStatus.PENDING):
    if value in (None, ""):
        return default
    valid_statuses = {choice.value for choice in SubscriptionStatus}
    if value not in valid_statuses:
        raise ApiValidationError({"status": "invalid"})
    return value


def validate_bool(value, field, *, default=None):
    if value is None:
        if default is None:
            raise ApiValidationError({field: "required"})
        return default
    if not isinstance(value, bool):
        raise ApiValidationError({field: "must_be_boolean"})
    return value


def validate_timestamp(value, field, *, default_now=False):
    if value in (None, ""):
        return timezone.now() if default_now else None
    if not isinstance(value, str):
        raise ApiValidationError({field: "must_be_iso_datetime"})
    parsed = parse_datetime(value)
    if parsed is None:
        raise ApiValidationError({field: "must_be_iso_datetime"})
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone=timezone.get_current_timezone())
    return parsed


def validate_email_validation_status(value):
    if value in (None, ""):
        return EmailValidationStatus.UNKNOWN
    valid_statuses = {choice.value for choice in EmailValidationStatus}
    if value not in valid_statuses:
        raise ApiValidationError({"email_validation.status": "invalid"})
    return value


def validate_reason(value, field):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiValidationError({field: "must_be_string"})
    return value.strip()


def contact_tags_payload(contact, audience):
    return sorted(
        ContactTag.objects.filter(contact=contact, tag__audience=audience).values_list("tag__slug", flat=True)
    )


def subscription_payload(subscription):
    if subscription is None:
        return {
            "subscribed": False,
            "status": None,
            "verified": False,
            "verified_at": None,
            "unsubscribed_at": None,
            "unsubscribe_reason": "",
        }

    return {
        "subscribed": subscription.status == SubscriptionStatus.SUBSCRIBED,
        "status": subscription.status,
        "verified": subscription.verified_at is not None,
        "verified_at": isoformat(subscription.verified_at),
        "unsubscribed_at": isoformat(subscription.unsubscribed_at),
        "unsubscribe_reason": subscription.unsubscribe_reason,
    }


def email_validation_payload(contact):
    if contact is None:
        return {
            "status": "unknown",
            "reason": "",
            "validated_at": None,
        }
    return {
        "status": contact.email_validation_status,
        "reason": contact.email_validation_reason,
        "validated_at": isoformat(contact.email_validated_at),
    }


def contact_status_payload(contact, audience, client, *, exists=True, requested_email=""):
    audience_subscription = None
    client_subscription = None
    if contact is not None:
        audience_subscription = Subscription.objects.filter(
            contact=contact,
            audience=audience,
            client__isnull=True,
        ).first()
        client_subscription = Subscription.objects.filter(
            contact=contact,
            audience=audience,
            client=client,
        ).first()

    visible = exists and contact is not None and client_subscription is not None
    if not visible:
        return {
            "contact_id": None,
            "email": normalize_email(requested_email or contact.email if contact else requested_email),
            "exists": False,
            "verified": False,
            "verified_at": None,
            "email_validation": email_validation_payload(None),
            "global_unsubscribed": False,
            "hard_bounced": False,
            "complained": False,
            "audience": {
                "slug": audience.slug,
                **subscription_payload(None),
            },
            "client": {
                "slug": client.slug,
                **subscription_payload(None),
            },
            "can_send_marketing": False,
            "can_send_transactional": False,
        }

    return {
        "contact_id": contact.id,
        "email": contact.normalized_email,
        "exists": True,
        "verified": contact.verified_at is not None,
        "verified_at": isoformat(contact.verified_at),
        "email_validation": email_validation_payload(contact),
        "global_unsubscribed": contact.global_unsubscribed_at is not None,
        "hard_bounced": contact.hard_bounced_at is not None,
        "complained": contact.complained_at is not None,
        "audience": {
            "slug": audience.slug,
            **subscription_payload(audience_subscription),
        },
        "client": {
            "slug": client.slug,
            **subscription_payload(client_subscription),
        },
        "can_send_marketing": is_verified_for_marketing(contact, audience_subscription, client_subscription)
        and is_marketing_email_allowed(contact, audience, client),
        "can_send_transactional": is_transactional_email_allowed(contact),
    }


def contact_payload(contact, audience, client, *, requested_email=""):
    return contact_status_payload(contact, audience, client, requested_email=requested_email) | {
        "tags": contact_tags_payload(contact, audience),
    }


def normalize_category_tag(value, field="tag"):
    if not isinstance(value, str) or not value.strip():
        raise ApiValidationError({field: "required"})
    tag = value.strip()
    try:
        validate_slug(tag)
    except ValidationError as exc:
        raise ApiValidationError({field: "invalid"}) from exc
    return tag


def category_label_for_tag(tag):
    return tag.replace("-", " ").title()


def category_tag_list(value):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw_tags = value.split(",")
    elif isinstance(value, list):
        raw_tags = value
    else:
        raise ApiValidationError({"category_tags": "invalid"})
    return [normalize_category_tag(tag, "category_tags") for tag in raw_tags if str(tag).strip()]


def category_preference_payload(preference, tag):
    if preference is None:
        return {
            "tag": tag,
            "label": category_label_for_tag(tag),
            "enabled": True,
        }
    return {
        "tag": preference.tag,
        "label": preference.label or category_label_for_tag(preference.tag),
        "enabled": preference.enabled,
    }


def preferences_suppression_payload(contact):
    if contact is None:
        return {
            "global_unsubscribed": False,
            "suppressed": False,
            "suppression_reasons": [],
        }
    reasons = []
    if contact.global_unsubscribed_at is not None:
        reasons.append("global_unsubscribed")
    if contact.hard_bounced_at is not None:
        reasons.append("hard_bounce")
    if contact.complained_at is not None:
        reasons.append("complaint")
    return {
        "global_unsubscribed": contact.global_unsubscribed_at is not None,
        "suppressed": bool(reasons),
        "suppression_reasons": reasons,
    }


def get_contact_preferences_for_client(data, authenticated_client):
    scope = validate_contact_scope(data, authenticated_client)
    requested_tags = category_tag_list(data.get("category_tags"))
    contact = Contact.objects.filter(normalized_email=normalize_email(scope.email)).first()
    preferences = {}
    if contact is not None:
        preferences = {
            preference.tag: preference
            for preference in CategoryPreference.objects.filter(
                contact=contact,
                audience=scope.audience,
                client=scope.client,
            )
        }
    if not requested_tags:
        requested_tags = sorted(preferences)

    return {
        "email": normalize_email(scope.email),
        "audience": scope.audience.slug,
        "client": scope.client.slug,
        "categories": [
            category_preference_payload(preferences.get(tag), tag)
            for tag in requested_tags
        ],
        **preferences_suppression_payload(contact),
    }


def validate_category_preference_input(data):
    categories = data.get("categories")
    if not isinstance(categories, list):
        raise ApiValidationError({"categories": "must_be_list"})
    parsed = []
    for index, item in enumerate(categories):
        if not isinstance(item, dict):
            raise ApiValidationError({f"categories.{index}": "must_be_object"})
        tag = normalize_category_tag(item.get("tag"), f"categories.{index}.tag")
        enabled = item.get("enabled")
        if not isinstance(enabled, bool):
            raise ApiValidationError({f"categories.{index}.enabled": "must_be_boolean"})
        label = item.get("label", "")
        if label is None:
            label = ""
        if not isinstance(label, str):
            raise ApiValidationError({f"categories.{index}.label": "must_be_string"})
        parsed.append(
            {
                "tag": tag,
                "enabled": enabled,
                "label": label.strip(),
            }
        )
    return parsed


@transaction.atomic
def update_contact_preferences_for_client(data, authenticated_client):
    scope = validate_contact_scope(data, authenticated_client)
    categories = validate_category_preference_input(data)
    contact, _ = upsert_contact(scope.email)
    suppression = preferences_suppression_payload(contact)
    if suppression["suppressed"] and any(category["enabled"] for category in categories):
        raise ApiValidationError(
            {"categories": "suppressed_contact_cannot_be_enabled"},
            status_code=409,
        )

    for category in categories:
        CategoryPreference.objects.update_or_create(
            contact=contact,
            audience=scope.audience,
            client=scope.client,
            tag=category["tag"],
            defaults={
                "label": category["label"],
                "enabled": category["enabled"],
                "updated_reason": data.get("reason", "") if isinstance(data.get("reason", ""), str) else "",
            },
        )
    return get_contact_preferences_for_client(
        {
            "email": scope.email,
            "audience": scope.audience.slug,
            "client": scope.client.slug,
            "category_tags": [category["tag"] for category in categories],
        },
        authenticated_client,
    )


def apply_validation_input(contact, data):
    validation = data.get("email_validation")
    if validation in (None, ""):
        return []
    if not isinstance(validation, dict):
        raise ApiValidationError({"email_validation": "must_be_object"})

    status = validate_email_validation_status(validation.get("status"))
    reason = validate_reason(validation.get("reason"), "email_validation.reason")
    if "validated_at" in validation:
        validated_at = validate_timestamp(validation.get("validated_at"), "email_validation.validated_at")
    elif status == EmailValidationStatus.UNKNOWN:
        validated_at = None
    elif contact.email_validation_status == status and contact.email_validation_reason == reason:
        validated_at = contact.email_validated_at or timezone.now()
    else:
        validated_at = timezone.now()

    updates = []
    for field, value in {
        "email_validation_status": status,
        "email_validation_reason": reason,
        "email_validated_at": validated_at,
    }.items():
        if getattr(contact, field) != value:
            setattr(contact, field, value)
            updates.append(field)
    return updates


def apply_suppression_input(contact, data):
    suppression = data.get("suppression")
    if suppression in (None, ""):
        return []
    if not isinstance(suppression, dict):
        raise ApiValidationError({"suppression": "must_be_object"})

    now = timezone.now()
    updates = []
    for payload_key, field_name in {
        "global_unsubscribed": "global_unsubscribed_at",
        "hard_bounced": "hard_bounced_at",
        "complained": "complained_at",
    }.items():
        if payload_key not in suppression:
            continue
        is_enabled = validate_bool(suppression.get(payload_key), f"suppression.{payload_key}")
        value = now if is_enabled else None
        if getattr(contact, field_name) != value and not (is_enabled and getattr(contact, field_name) is not None):
            setattr(contact, field_name, value)
            updates.append(field_name)
    return updates


@transaction.atomic
def upsert_contact_for_client(data, authenticated_client):
    scope = validate_contact_scope(data, authenticated_client)
    tags = validate_tags(data.get("tags"))
    status = validate_status(data.get("status"))
    verified = data.get("verified", False)
    if not isinstance(verified, bool):
        raise ApiValidationError({"verified": "must_be_boolean"})

    contact, _ = upsert_contact(scope.email)
    contact_updates = apply_validation_input(contact, data)
    contact_updates.extend(apply_suppression_input(contact, data))
    if contact_updates:
        contact_updates.append("updated_at")
        contact.save(update_fields=sorted(set(contact_updates)))

    existing_subscription = Subscription.objects.filter(
        contact=contact,
        audience=scope.audience,
        client=scope.client,
    ).first()
    verified_at = None
    if verified:
        verified_at = existing_subscription.verified_at if existing_subscription else timezone.now()
        verified_at = verified_at or timezone.now()
    unsubscribed_at = None
    if status == SubscriptionStatus.UNSUBSCRIBED:
        unsubscribed_at = existing_subscription.unsubscribed_at if existing_subscription else timezone.now()
        unsubscribed_at = unsubscribed_at or timezone.now()

    subscription, _ = Subscription.objects.update_or_create(
        contact=contact,
        audience=scope.audience,
        client=scope.client,
        defaults={
            "status": status,
            "verified_at": verified_at,
            "unsubscribed_at": unsubscribed_at,
            "unsubscribe_reason": "",
        },
    )
    if (
        existing_subscription is not None
        and existing_subscription.status == SubscriptionStatus.UNSUBSCRIBED
        and status == SubscriptionStatus.SUBSCRIBED
    ):
        event = EmailEvent.objects.create(
            contact=contact,
            client=scope.client,
            audience=scope.audience,
            event_type=EmailEventType.SUBSCRIBE,
            metadata={"source": "api"},
        )
        emit_cmp_contact_event(event)

    for tag_name in tags:
        assign_tag(contact, scope.audience, tag_name)

    return contact_payload(contact, scope.audience, scope.client, requested_email=scope.email)


def get_contact_status_for_client(data, authenticated_client):
    scope = validate_contact_scope(data, authenticated_client)
    contact = Contact.objects.filter(normalized_email=normalize_email(scope.email)).first()
    return contact_status_payload(contact, scope.audience, scope.client, requested_email=scope.email)


@transaction.atomic
def subscribe_for_client(data, authenticated_client):
    scope = validate_contact_scope(data, authenticated_client)
    tags = validate_tags(data.get("tags"))
    contact, _ = upsert_contact(scope.email)
    subscribe_contact(contact, scope.audience, scope.client)

    for tag_name in tags:
        assign_tag(contact, scope.audience, tag_name)

    return contact_payload(contact, scope.audience, scope.client, requested_email=scope.email)


@transaction.atomic
def unsubscribe_for_client(data, authenticated_client):
    scope_name = data.get("scope")
    if scope_name not in {"client", "audience", "global"}:
        raise ApiValidationError({"scope": "invalid"})

    scope = validate_contact_scope(data, authenticated_client)
    reason = data.get("reason", "")
    if reason is not None and not isinstance(reason, str):
        raise ApiValidationError({"reason": "must_be_string"})

    contact, _ = upsert_contact(scope.email)
    reason = reason or ""

    if scope_name == "global":
        contact.global_unsubscribed_at = contact.global_unsubscribed_at or timezone.now()
        contact.save(update_fields=["global_unsubscribed_at", "updated_at"])
    elif scope_name == "audience":
        unsubscribe_contact(contact, scope.audience, reason=reason)
    else:
        unsubscribe_contact(contact, scope.audience, scope.client, reason=reason)

    return contact_status_payload(contact, scope.audience, scope.client, requested_email=scope.email) | {
        "scope": scope_name
    }


def validate_existing_contact_scope(contact_id, data, authenticated_client):
    scope = validate_contact_scope(data, authenticated_client, require_email=False)
    contact = Contact.objects.filter(id=contact_id).first()
    if contact is None:
        raise ApiValidationError({"contact_id": "not_found"}, status_code=404)

    has_client_subscription = Subscription.objects.filter(
        contact=contact,
        audience=scope.audience,
        client=scope.client,
    ).exists()
    if not has_client_subscription:
        raise ApiValidationError({"contact_id": "not_found"}, status_code=404)
    return contact, scope


@transaction.atomic
def replace_contact_tags_for_client(contact_id, data, authenticated_client):
    contact, scope = validate_existing_contact_scope(contact_id, data, authenticated_client)
    tag_names = validate_tags(data.get("tags"))
    desired_slugs = {normalize_tag_slug(tag_name) for tag_name in tag_names}
    desired_slugs.discard("")

    existing = ContactTag.objects.filter(contact=contact, tag__audience=scope.audience)
    existing.exclude(tag__slug__in=desired_slugs).delete()
    for tag_name in tag_names:
        assign_tag(contact, scope.audience, tag_name)

    return contact_payload(contact, scope.audience, scope.client, requested_email=contact.email)


@transaction.atomic
def add_contact_tag_for_client(contact_id, tag_slug, data, authenticated_client):
    contact, scope = validate_existing_contact_scope(contact_id, data, authenticated_client)
    tag_slug = validate_tag_slug(tag_slug)
    assign_tag(contact, scope.audience, tag_slug, slug=tag_slug)
    return contact_payload(contact, scope.audience, scope.client, requested_email=contact.email)


@transaction.atomic
def remove_contact_tag_for_client(contact_id, tag_slug, data, authenticated_client):
    contact, scope = validate_existing_contact_scope(contact_id, data, authenticated_client)
    tag_slug = validate_tag_slug(tag_slug)
    ContactTag.objects.filter(contact=contact, tag__audience=scope.audience, tag__slug=tag_slug).delete()
    return contact_payload(contact, scope.audience, scope.client, requested_email=contact.email)


@transaction.atomic
def update_contact_verification_for_client(contact_id, data, authenticated_client):
    contact, scope = validate_existing_contact_scope(contact_id, data, authenticated_client)
    verified = validate_bool(data.get("verified"), "verified")
    if verified and "verified_at" in data:
        verified_at = validate_timestamp(data.get("verified_at"), "verified_at", default_now=True)
    elif verified:
        verified_at = contact.verified_at or timezone.now()
    else:
        verified_at = None

    if contact.verified_at != verified_at:
        contact.verified_at = verified_at
        contact.save(update_fields=["verified_at", "updated_at"])

    subscription = Subscription.objects.get(contact=contact, audience=scope.audience, client=scope.client)
    subscription_verified_at = (
        verified_at if not verified or subscription.verified_at is None else subscription.verified_at
    )
    if subscription.verified_at != subscription_verified_at:
        Subscription.objects.filter(pk=subscription.pk).update(
            verified_at=subscription_verified_at,
            updated_at=timezone.now(),
        )
    return contact_payload(contact, scope.audience, scope.client, requested_email=contact.email)


@transaction.atomic
def update_contact_validation_for_client(contact_id, data, authenticated_client):
    contact, scope = validate_existing_contact_scope(contact_id, data, authenticated_client)
    status = validate_email_validation_status(data.get("status"))
    reason = validate_reason(data.get("reason"), "reason")
    if "validated_at" in data:
        validated_at = validate_timestamp(data.get("validated_at"), "validated_at")
    elif status == EmailValidationStatus.UNKNOWN:
        validated_at = None
    elif contact.email_validation_status == status and contact.email_validation_reason == reason:
        validated_at = contact.email_validated_at or timezone.now()
    else:
        validated_at = timezone.now()

    contact.email_validation_status = status
    contact.email_validation_reason = reason
    contact.email_validated_at = validated_at
    contact.save(
        update_fields=[
            "email_validation_status",
            "email_validation_reason",
            "email_validated_at",
            "updated_at",
        ]
    )
    return contact_payload(contact, scope.audience, scope.client, requested_email=contact.email)


@transaction.atomic
def update_contact_suppression_for_client(contact_id, data, authenticated_client):
    contact, scope = validate_existing_contact_scope(contact_id, data, authenticated_client)
    updates = apply_suppression_input(contact, {"suppression": data})
    reason = validate_reason(data.get("reason"), "reason")
    if updates:
        contact.save(update_fields=sorted(set(updates + ["updated_at"])))
        event_map = {
            "global_unsubscribed_at": EmailEventType.UNSUBSCRIBE,
            "hard_bounced_at": EmailEventType.BOUNCE,
            "complained_at": EmailEventType.COMPLAINT,
        }
        for field in updates:
            if getattr(contact, field) is not None:
                event = EmailEvent.objects.create(
                    contact=contact,
                    client=scope.client,
                    audience=scope.audience,
                    event_type=event_map[field],
                    metadata={"source": "api", "reason": reason},
                )
                emit_cmp_contact_event(event)
            elif field == "global_unsubscribed_at":
                event = EmailEvent.objects.create(
                    contact=contact,
                    client=scope.client,
                    audience=scope.audience,
                    event_type=EmailEventType.SUBSCRIBE,
                    metadata={"source": "api", "reason": reason},
                )
                emit_cmp_contact_event(event)
    return contact_payload(contact, scope.audience, scope.client, requested_email=contact.email)


def validate_history_limit(value):
    if value in (None, ""):
        return 50
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiValidationError({"limit": "must_be_integer"}) from exc
    if limit < 1 or limit > 100:
        raise ApiValidationError({"limit": "must_be_between_1_and_100"})
    return limit


def validate_cursor(value):
    if value in (None, ""):
        return None
    try:
        cursor = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiValidationError({"cursor": "must_be_integer"}) from exc
    if cursor < 1:
        raise ApiValidationError({"cursor": "must_be_positive"})
    return cursor


def safe_metadata(metadata):
    if not isinstance(metadata, dict):
        return {}
    allowed = {"reason", "error", "scope", "ses_message_id", "bounce_type", "complaint_feedback_type", "source"}
    return {key: value for key, value in metadata.items() if key in allowed and value not in (None, "", [], {})}


def campaign_history_item(recipient):
    return {
        "type": "campaign_recipient",
        "id": recipient.id,
        "created_at": isoformat(recipient.created_at),
        "campaign": {
            "id": recipient.campaign_id,
            "subject": recipient.campaign.subject,
            "audience": recipient.campaign.audience.slug,
            "client": recipient.campaign.client.slug,
        },
        "email": recipient.email,
        "status": recipient.status,
        "skip_reason": recipient.skip_reason,
        "sent_at": isoformat(recipient.sent_at),
        "delivered_at": isoformat(recipient.delivered_at),
        "first_opened_at": isoformat(recipient.first_opened_at),
        "first_clicked_at": isoformat(recipient.first_clicked_at),
        "open_count": recipient.open_count,
        "click_count": recipient.click_count,
        "last_error": recipient.last_error,
    }


def transactional_history_item(message):
    return {
        "type": "transactional_message",
        "id": message.id,
        "created_at": isoformat(message.created_at),
        "client": message.client.slug,
        "email": message.email,
        "from_email": message.from_email_id,
        "from_email_address": message.from_email,
        "template_key": message.template_key,
        "status": message.status,
        "subject": message.subject,
        "sent_at": isoformat(message.sent_at),
        "delivered_at": isoformat(message.delivered_at),
        "first_opened_at": isoformat(message.first_opened_at),
        "first_clicked_at": isoformat(message.first_clicked_at),
        "open_count": message.open_count,
        "click_count": message.click_count,
        "last_error": message.last_error,
    }


def transactional_message_status_item(message):
    return transactional_history_item(message) | {
        "contact_id": message.contact_id,
        "idempotency_key": message.idempotency_key,
        "ses_message_id": message.ses_message_id,
        "updated_at": isoformat(message.updated_at),
    }


def event_history_item(event):
    return {
        "type": "email_event",
        "id": event.id,
        "created_at": isoformat(event.created_at),
        "event_type": event.event_type,
        "client": event.client.slug if event.client_id else None,
        "audience": event.audience.slug if event.audience_id else None,
        "campaign_id": event.campaign_id,
        "campaign_recipient_id": event.campaign_recipient_id,
        "transactional_message_id": event.transactional_message_id,
        "metadata": safe_metadata(event.metadata),
    }


def get_transactional_message_status_for_client(message_id, authenticated_client):
    message = (
        TransactionalMessage.objects.select_related("client", "contact")
        .filter(id=message_id, client=authenticated_client)
        .first()
    )
    if message is None:
        raise ApiValidationError({"message_id": "not_found"}, status_code=404)

    event_items = [
        event_history_item(event)
        for event in EmailEvent.objects.filter(transactional_message=message)
        .filter(Q(client=authenticated_client) | Q(client__isnull=True))
        .select_related("client", "audience")
        .order_by("-id")
    ]

    return {
        "message": transactional_message_status_item(message),
        "events": event_items,
    }


def get_contact_history_for_client(contact_id, data, authenticated_client):
    contact, scope = validate_existing_contact_scope(contact_id, data, authenticated_client)
    limit = validate_history_limit(data.get("limit"))
    cursor = validate_cursor(data.get("cursor"))

    campaign_items = [
        campaign_history_item(recipient)
        for recipient in CampaignRecipient.objects.filter(
            contact=contact,
            campaign__audience=scope.audience,
            campaign__client=scope.client,
        )
        .select_related("campaign", "campaign__audience", "campaign__client")
        .order_by("-created_at", "-id")[:limit]
    ]
    transactional_items = [
        transactional_history_item(message)
        for message in TransactionalMessage.objects.filter(contact=contact, client=scope.client)
        .select_related("client")
        .order_by("-created_at", "-id")[:limit]
    ]
    event_queryset = EmailEvent.objects.filter(contact=contact).filter(
        (Q(client=scope.client) | Q(client__isnull=True)) & (Q(audience=scope.audience) | Q(audience__isnull=True))
    )
    if cursor is not None:
        event_queryset = event_queryset.filter(id__lt=cursor)
    event_items = [
        event_history_item(event)
        for event in event_queryset.select_related("client", "audience").order_by("-id")[: limit + 1]
    ]
    next_cursor = None
    if len(event_items) > limit:
        next_cursor = event_items[limit - 1]["id"]
        event_items = event_items[:limit]

    return {
        "contact_id": contact.id,
        "email": contact.normalized_email,
        "audience": scope.audience.slug,
        "client": scope.client.slug,
        "campaign_recipients": campaign_items,
        "transactional_messages": transactional_items,
        "events": event_items,
        "next_cursor": str(next_cursor) if next_cursor else None,
    }


def isoformat(value):
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")
