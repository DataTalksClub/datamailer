from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from mailing.models import (
    Audience,
    Client,
    Contact,
    ContactTag,
    EmailValidationStatus,
    Subscription,
    SubscriptionStatus,
    Tag,
)

NON_DELIVERABLE_EMAIL_VALIDATION_STATUSES = {
    EmailValidationStatus.INVALID_SYNTAX,
    EmailValidationStatus.NO_MX,
    EmailValidationStatus.DISPOSABLE,
    EmailValidationStatus.RISKY,
    EmailValidationStatus.MANUALLY_INVALID,
}

DELIVERABLE_EMAIL_VALIDATION_STATUSES = {
    EmailValidationStatus.VALID,
    EmailValidationStatus.EXTERNALLY_VALIDATED,
}


def normalize_email(email):
    return email.strip().casefold()


@transaction.atomic
def upsert_contact(
    email,
    *,
    verified_at=None,
    email_validation_status=None,
    email_validation_reason=None,
    email_validated_at=None,
    global_unsubscribed_at=None,
    hard_bounced_at=None,
    complained_at=None,
):
    normalized_email = normalize_email(email)
    defaults = {"email": email.strip()}
    for field, value in {
        "verified_at": verified_at,
        "email_validation_status": email_validation_status,
        "email_validation_reason": email_validation_reason,
        "email_validated_at": email_validated_at,
        "global_unsubscribed_at": global_unsubscribed_at,
        "hard_bounced_at": hard_bounced_at,
        "complained_at": complained_at,
    }.items():
        if value is not None:
            defaults[field] = value

    contact, created = Contact.objects.update_or_create(normalized_email=normalized_email, defaults=defaults)
    return contact, created


@transaction.atomic
def subscribe_contact(contact, audience, client=None, *, verified_at=None):
    verified_at = verified_at or timezone.now()
    subscription, _ = Subscription.objects.update_or_create(
        contact=contact,
        audience=audience,
        client=client,
        defaults={
            "status": SubscriptionStatus.SUBSCRIBED,
            "verified_at": verified_at,
            "unsubscribed_at": None,
            "unsubscribe_reason": "",
        },
    )
    return subscription


@transaction.atomic
def unsubscribe_contact(contact, audience, client=None, *, reason="", unsubscribed_at=None):
    unsubscribed_at = unsubscribed_at or timezone.now()
    subscription, _ = Subscription.objects.update_or_create(
        contact=contact,
        audience=audience,
        client=client,
        defaults={
            "status": SubscriptionStatus.UNSUBSCRIBED,
            "unsubscribed_at": unsubscribed_at,
            "unsubscribe_reason": reason,
        },
    )
    return subscription


@transaction.atomic
def assign_tag(contact, audience, tag_name, *, slug=None):
    tag_slug = slug or normalize_tag_slug(tag_name)
    tag, _ = Tag.objects.get_or_create(
        audience=audience,
        slug=tag_slug,
        defaults={"name": tag_name.strip()},
    )
    membership, _ = ContactTag.objects.get_or_create(contact=contact, tag=tag)
    return membership


def normalize_tag_slug(value):
    return slugify(value.strip())


@dataclass(frozen=True)
class SuppressionState:
    contact_id: int
    global_unsubscribed_at: object | None
    hard_bounced_at: object | None
    complained_at: object | None

    @property
    def is_globally_unsubscribed(self):
        return self.global_unsubscribed_at is not None

    @property
    def is_hard_bounced(self):
        return self.hard_bounced_at is not None

    @property
    def is_complained(self):
        return self.complained_at is not None

    @property
    def has_hard_suppression(self):
        return self.is_hard_bounced or self.is_complained

    @property
    def has_marketing_suppression(self):
        return self.is_globally_unsubscribed or self.has_hard_suppression


def get_contact_suppression_state(contact):
    return SuppressionState(
        contact_id=contact.id,
        global_unsubscribed_at=contact.global_unsubscribed_at,
        hard_bounced_at=contact.hard_bounced_at,
        complained_at=contact.complained_at,
    )


def is_transactional_email_allowed(contact):
    return not get_contact_suppression_state(contact).has_hard_suppression


def has_invalid_email_validation(contact):
    return contact.email_validation_status in NON_DELIVERABLE_EMAIL_VALIDATION_STATUSES


def is_marketing_email_allowed(contact, audience, client=None):
    suppression = get_contact_suppression_state(contact)
    if suppression.has_marketing_suppression:
        return False
    if has_invalid_email_validation(contact):
        return False

    return Subscription.objects.filter(
        contact=contact,
        audience=audience,
        client=client,
        status=SubscriptionStatus.SUBSCRIBED,
    ).exists()


def get_subscription_for_slugs(email, audience_slug, client_slug=None):
    normalized_email = normalize_email(email)
    filters = {
        "contact__normalized_email": normalized_email,
        "audience__slug": audience_slug,
    }
    if client_slug is None:
        filters["client__isnull"] = True
    else:
        filters["client__slug"] = client_slug

    return (
        Subscription.objects.select_related("contact", "audience", "client")
        .filter(**filters)
        .first()
    )


def get_audience_client_for_slugs(audience_slug, client_slug):
    return (
        Audience.objects.filter(slug=audience_slug).first(),
        Client.objects.filter(slug=client_slug).first(),
    )
