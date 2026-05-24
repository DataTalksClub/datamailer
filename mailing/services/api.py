from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from mailing.models import Audience, Contact, ContactTag, Subscription, SubscriptionStatus
from mailing.services.contacts import (
    assign_tag,
    is_marketing_email_allowed,
    is_transactional_email_allowed,
    normalize_email,
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


def validate_contact_scope(data, authenticated_client, *, require_email=True):
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
    if not isinstance(client_slug, str) or not client_slug.strip():
        errors["client"] = "required"
    elif client_slug.strip() != authenticated_client.slug:
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

    return ScopedRequest(email=email.strip(), audience=audience, client=authenticated_client)


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


def validate_status(value, *, default=SubscriptionStatus.PENDING):
    if value in (None, ""):
        return default
    valid_statuses = {choice.value for choice in SubscriptionStatus}
    if value not in valid_statuses:
        raise ApiValidationError({"status": "invalid"})
    return value


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


@transaction.atomic
def upsert_contact_for_client(data, authenticated_client):
    scope = validate_contact_scope(data, authenticated_client)
    tags = validate_tags(data.get("tags"))
    status = validate_status(data.get("status"))
    verified = data.get("verified", False)
    if not isinstance(verified, bool):
        raise ApiValidationError({"verified": "must_be_boolean"})

    contact, _ = upsert_contact(scope.email)
    verified_at = timezone.now() if verified else None

    subscription, _ = Subscription.objects.update_or_create(
        contact=contact,
        audience=scope.audience,
        client=scope.client,
        defaults={
            "status": status,
            "verified_at": verified_at if verified else None,
            "unsubscribed_at": timezone.now() if status == SubscriptionStatus.UNSUBSCRIBED else None,
            "unsubscribe_reason": "",
        },
    )

    for tag_name in tags:
        assign_tag(contact, scope.audience, tag_name)

    return contact_status_payload(contact, scope.audience, scope.client, requested_email=scope.email) | {
        "tags": sorted(
            ContactTag.objects.filter(contact=contact, tag__audience=scope.audience).values_list("tag__slug", flat=True)
        )
    }


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

    return contact_status_payload(contact, scope.audience, scope.client, requested_email=scope.email) | {
        "tags": sorted(
            ContactTag.objects.filter(contact=contact, tag__audience=scope.audience).values_list("tag__slug", flat=True)
        )
    }


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
