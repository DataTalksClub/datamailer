from django.db import transaction
from django.utils import timezone

from mailing.models import (
    Audience,
    Client,
    ClientApiKey,
    ContactTag,
    EmailValidationStatus,
    OperatorAudit,
    Subscription,
    SubscriptionStatus,
    Tag,
)
from mailing.services.auth import create_client_api_key
from mailing.services.contacts import normalize_tag_slug

SECRET_METADATA_KEYS = {"api_key", "raw_api_key", "api_key_hash", "token", "secret", "password"}


def safe_metadata(metadata):
    cleaned = {}
    for key, value in (metadata or {}).items():
        if key in SECRET_METADATA_KEYS:
            continue
        if value in (None, "", [], {}):
            continue
        cleaned[key] = value
    return cleaned


def audit(actor, action, target, metadata=None):
    return OperatorAudit.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        target_type=target.__class__.__name__.lower(),
        target_id=target.id,
        metadata=safe_metadata(metadata),
    )


def latest_audits_for(target, limit=25):
    return OperatorAudit.objects.filter(
        target_type=target.__class__.__name__.lower(),
        target_id=target.id,
    ).select_related("actor")[:limit]


@transaction.atomic
def create_or_update_audience(*, actor, audience=None, organization, name, slug):
    if audience is None:
        audience = Audience.objects.create(organization=organization, name=name, slug=slug)
        audit(actor, "audience.create", audience, {"organization": organization.slug, "slug": slug})
        return audience
    changed = {}
    if audience.name != name:
        changed["name"] = [audience.name, name]
        audience.name = name
    if audience.slug != slug:
        changed["slug"] = [audience.slug, slug]
        audience.slug = slug
    if changed:
        audience.save(update_fields=["name", "slug"])
        audit(actor, "audience.update", audience, changed)
    return audience


@transaction.atomic
def create_or_update_tag(*, actor, tag=None, audience, name, slug):
    if tag is None:
        tag = Tag.objects.create(audience=audience, name=name, slug=slug)
        audit(actor, "tag.create", tag, {"audience": audience.slug, "slug": slug})
        return tag
    changed = {}
    if tag.name != name:
        changed["name"] = [tag.name, name]
        tag.name = name
    if tag.slug != slug:
        changed["slug"] = [tag.slug, slug]
        tag.slug = slug
    if changed:
        tag.save(update_fields=["name", "slug"])
        audit(actor, "tag.update", tag, changed | {"audience": audience.slug})
    return tag


@transaction.atomic
def create_or_update_client(*, actor, client=None, organization, name, slug, is_active):
    if client is None:
        client = Client.objects.create(organization=organization, name=name, slug=slug, is_active=is_active)
        audit(actor, "client.create", client, {"organization": organization.slug, "slug": slug, "is_active": is_active})
        return client
    changed = {}
    for field, value in {"name": name, "slug": slug, "is_active": is_active}.items():
        old = getattr(client, field)
        if old != value:
            changed[field] = [old, value]
            setattr(client, field, value)
    if changed:
        client.save(update_fields=sorted(set(changed) | {"updated_at"}))
        audit(actor, "client.update", client, changed)
    return client


@transaction.atomic
def create_api_key(*, actor, client, name, notes=""):
    api_key, raw_key = create_client_api_key(client=client, name=name, notes=notes)
    audit(
        actor,
        "client.api_key.create",
        client,
        {"client": client.slug, "key_id": api_key.id, "key_prefix": api_key.display_prefix, "key_name": api_key.name},
    )
    return api_key, raw_key


@transaction.atomic
def revoke_api_key(*, actor, api_key):
    if api_key.revoked_at:
        return False
    api_key.revoked_at = timezone.now()
    api_key.save(update_fields=["revoked_at", "updated_at"])
    audit(
        actor,
        "client.api_key.revoke",
        api_key.client,
        {
            "client": api_key.client.slug,
            "key_id": api_key.id,
            "key_prefix": api_key.display_prefix,
            "key_name": api_key.name,
        },
    )
    return True


def client_api_keys_for_detail(client):
    return ClientApiKey.objects.filter(client=client).order_by("revoked_at", "name", "created_at")


@transaction.atomic
def update_contact_state(*, actor, contact, verified_state, validation_status, validation_reason, suppression_flags):
    now = timezone.now()
    changed = {}
    verified_at = contact.verified_at or now if verified_state == "verified" else None if verified_state == "unverified" else contact.verified_at
    if contact.verified_at != verified_at:
        changed["verified_at"] = [contact.verified_at.isoformat() if contact.verified_at else None, bool(verified_at)]
        contact.verified_at = verified_at

    if validation_status != contact.email_validation_status or validation_reason != contact.email_validation_reason:
        changed["email_validation"] = [contact.email_validation_status, validation_status]
        contact.email_validation_status = validation_status
        contact.email_validation_reason = validation_reason
        contact.email_validated_at = None if validation_status == EmailValidationStatus.UNKNOWN else now

    for flag, field in {
        "global_unsubscribed": "global_unsubscribed_at",
        "hard_bounced": "hard_bounced_at",
        "complained": "complained_at",
    }.items():
        desired = now if suppression_flags.get(flag) else None
        current = getattr(contact, field)
        if bool(current) != bool(desired):
            changed[field] = [bool(current), bool(desired)]
            setattr(contact, field, desired)

    if changed:
        contact.save(
            update_fields=[
                "verified_at",
                "email_validation_status",
                "email_validation_reason",
                "email_validated_at",
                "global_unsubscribed_at",
                "hard_bounced_at",
                "complained_at",
                "updated_at",
            ]
        )
        audit(actor, "contact.state.update", contact, changed)
    return bool(changed)


@transaction.atomic
def update_subscription(*, actor, contact, audience, client, status, unsubscribe_reason, verified):
    if client and client.organization_id != audience.organization_id:
        raise ValueError("Client must belong to the selected audience organization.")
    now = timezone.now()
    subscription, created = Subscription.objects.get_or_create(contact=contact, audience=audience, client=client)
    old = {
        "status": subscription.status,
        "unsubscribe_reason": subscription.unsubscribe_reason,
        "verified": subscription.verified_at is not None,
    }
    subscription.status = status
    subscription.verified_at = now if verified else None
    subscription.unsubscribed_at = now if status == SubscriptionStatus.UNSUBSCRIBED else None
    subscription.unsubscribe_reason = unsubscribe_reason if status == SubscriptionStatus.UNSUBSCRIBED else ""
    changed = created or old != {
        "status": subscription.status,
        "unsubscribe_reason": subscription.unsubscribe_reason,
        "verified": subscription.verified_at is not None,
    }
    if changed:
        subscription.save()
        audit(
            actor,
            "contact.subscription.update",
            contact,
            {
                "audience": audience.slug,
                "client": client.slug if client else "audience",
                "status": status,
                "created": created,
            },
        )
    return subscription, changed


@transaction.atomic
def add_contact_tag(*, actor, contact, audience, tag=None, name="", slug=""):
    if tag is None:
        tag_slug = normalize_tag_slug(slug or name)
        tag, _ = Tag.objects.get_or_create(audience=audience, slug=tag_slug, defaults={"name": name or tag_slug})
    elif tag.audience_id != audience.id:
        raise ValueError("Tag must belong to the selected audience.")
    membership, created = ContactTag.objects.get_or_create(contact=contact, tag=tag)
    if created:
        audit(actor, "contact.tag.add", contact, {"audience": audience.slug, "tag": tag.slug})
    return membership, created


@transaction.atomic
def remove_contact_tag(*, actor, contact, tag):
    deleted, _ = ContactTag.objects.filter(contact=contact, tag=tag).delete()
    if deleted:
        audit(actor, "contact.tag.remove", contact, {"audience": tag.audience.slug, "tag": tag.slug})
    return bool(deleted)
