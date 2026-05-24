from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mailing.models import (
    Audience,
    CampaignRecipient,
    Contact,
    ContactTag,
    EmailEvent,
    EmailEventType,
    EmailValidationStatus,
    Subscription,
    SubscriptionStatus,
    TransactionalMessage,
)
from mailing.services.contacts import (
    assign_tag,
    is_marketing_email_allowed,
    is_transactional_email_allowed,
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

    return contact_status_payload(contact, scope.audience, scope.client, requested_email=scope.email) | {"scope": scope_name}


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
    subscription_verified_at = verified_at if not verified or subscription.verified_at is None else subscription.verified_at
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
                EmailEvent.objects.create(
                    contact=contact,
                    client=scope.client,
                    audience=scope.audience,
                    event_type=event_map[field],
                    metadata={"source": "api", "reason": reason},
                )
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
        for event in event_queryset.select_related("client", "audience")
        .order_by("-id")[: limit + 1]
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


def is_verified_for_marketing(contact, audience_subscription, client_subscription):
    return any(
        [
            contact.verified_at is not None,
            audience_subscription is not None and audience_subscription.verified_at is not None,
            client_subscription is not None and client_subscription.verified_at is not None,
        ]
    )
