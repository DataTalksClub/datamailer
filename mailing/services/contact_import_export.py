import csv
import io
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Q
from django.utils.dateparse import parse_datetime

from mailing.models import Contact, ContactTag, EmailValidationStatus, Subscription, SubscriptionStatus
from mailing.services.api import (
    ApiValidationError,
    contact_payload,
    isoformat,
    upsert_contact_for_client,
    validate_bool,
    validate_contact_scope,
)
from mailing.services.contacts import normalize_email, normalize_tag_slug

CSV_COLUMNS = [
    "email",
    "audience",
    "client",
    "tags",
    "subscription_status",
    "verified",
    "verified_at",
    "email_validation_status",
    "email_validation_reason",
    "email_validated_at",
    "global_unsubscribed",
    "hard_bounced",
    "complained",
    "unsubscribed",
    "unsubscribed_at",
    "unsubscribe_reason",
    "updated_at",
]

TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off", ""}


@dataclass(frozen=True)
class ExportScope:
    audience: object
    client: object


def base_counts():
    return {
        "total": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "invalid": 0,
    }


def bulk_import_contacts_for_client(data, authenticated_client):
    if not isinstance(data.get("contacts"), list):
        raise ApiValidationError({"contacts": "must_be_list"})

    dry_run = validate_bool(data.get("dry_run"), "dry_run", default=False)
    if "idempotency_key" in data and not isinstance(data.get("idempotency_key"), str):
        raise ApiValidationError({"idempotency_key": "must_be_string"})

    counts = base_counts()
    results = []
    errors = []
    seen = {}
    for index, raw_item in enumerate(data["contacts"]):
        item_number = index + 1
        counts["total"] += 1
        if not isinstance(raw_item, dict):
            counts["invalid"] += 1
            errors.append({"index": index, "item": item_number, "errors": {"item": "must_be_object"}})
            continue

        item = data_for_item(data, raw_item)
        try:
            scope_key = import_scope_key(item, authenticated_client)
        except ApiValidationError as exc:
            normalized = normalize_email(str(item.get("email", ""))) if item.get("email") else ""
            counts["invalid"] += 1
            errors.append({"index": index, "item": item_number, "email": normalized, "errors": exc.errors})
            continue

        normalized = scope_key[0]
        if scope_key in seen:
            counts["skipped"] += 1
            results.append(
                {
                    "index": index,
                    "item": item_number,
                    "email": normalized,
                    "action": "skipped",
                    "reason": "duplicate_input",
                    "kept_item": seen[scope_key],
                }
            )
            continue

        try:
            before = snapshot_item(item, authenticated_client)
            if dry_run:
                validate_contact_scope(item, authenticated_client)
                counts["skipped"] += 1
                action = "would_update" if before is not None else "would_create"
                results.append({"index": index, "item": item_number, "email": normalized, "action": action})
            else:
                payload = upsert_contact_for_client(item, authenticated_client)
                after = snapshot_item(item, authenticated_client)
                action = import_action(before, after)
                counts[action] += 1
                results.append({"index": index, "item": item_number, "action": action, "contact": payload})
            seen[scope_key] = item_number
        except ApiValidationError as exc:
            counts["invalid"] += 1
            errors.append({"index": index, "item": item_number, "email": normalized, "errors": exc.errors})

    return {
        "dry_run": dry_run,
        "idempotency_key": data.get("idempotency_key", ""),
        "counts": counts,
        "results": results,
        "errors": errors,
    }


def import_scope_key(item, authenticated_client):
    scope = validate_contact_scope(item, authenticated_client)
    return (normalize_email(scope.email), scope.audience.id, scope.client.id)


def data_for_item(parent, item):
    merged = dict(item)
    for field in ("audience", "client"):
        if field not in merged and field in parent:
            merged[field] = parent[field]
    if "subscription_status" in merged and "status" not in merged:
        merged["status"] = merged["subscription_status"]
    return merged


def snapshot_item(item, authenticated_client):
    try:
        scope = validate_contact_scope(item, authenticated_client)
    except ApiValidationError:
        return None
    contact = Contact.objects.filter(normalized_email=normalize_email(scope.email)).first()
    if contact is None:
        return None
    subscription = Subscription.objects.filter(contact=contact, audience=scope.audience, client=scope.client).first()
    if subscription is None:
        return None
    return {
        "email": contact.email,
        "verified_at": contact.verified_at,
        "email_validation_status": contact.email_validation_status,
        "email_validation_reason": contact.email_validation_reason,
        "email_validated_at": contact.email_validated_at,
        "global_unsubscribed_at": contact.global_unsubscribed_at,
        "hard_bounced_at": contact.hard_bounced_at,
        "complained_at": contact.complained_at,
        "subscription_status": subscription.status,
        "subscription_verified_at": subscription.verified_at,
        "unsubscribed_at": subscription.unsubscribed_at,
        "unsubscribe_reason": subscription.unsubscribe_reason,
        "tags": tuple(contact_tags(contact, scope.audience)),
    }


def import_action(before, after):
    if before is None and after is not None:
        return "created"
    if before == after:
        return "unchanged"
    return "updated"


def contact_tags(contact, audience):
    return sorted(
        ContactTag.objects.filter(contact=contact, tag__audience=audience).values_list("tag__slug", flat=True)
    )


def parse_csv_import(csv_text, *, audience_slug="", client_slug=""):
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise ApiValidationError({"csv": "missing_header"})

    contacts = []
    errors = []
    for row_number, row in enumerate(reader, start=2):
        item, row_errors = csv_row_to_item(row, row_number, audience_slug=audience_slug, client_slug=client_slug)
        if row_errors:
            errors.append({"row": row_number, "errors": row_errors})
        else:
            contacts.append(item)
    return contacts, errors


def csv_row_to_item(row, row_number, *, audience_slug="", client_slug=""):
    errors = {}
    email = clean(row.get("email"))
    if not email:
        errors["email"] = "required"
    else:
        try:
            validate_email(email)
        except ValidationError:
            errors["email"] = "invalid"

    audience = clean(row.get("audience")) or audience_slug
    client = clean(row.get("client")) or client_slug
    if not audience:
        errors["audience"] = "required"
    if not client:
        errors["client"] = "required"

    status = clean(row.get("subscription_status") or row.get("status")).casefold() or SubscriptionStatus.PENDING
    if status not in {choice.value for choice in SubscriptionStatus}:
        errors["subscription_status"] = "invalid"

    validation_status = clean(row.get("email_validation_status")).casefold()
    if validation_status and validation_status not in {choice.value for choice in EmailValidationStatus}:
        errors["email_validation_status"] = "invalid"

    item = {
        "email": email,
        "audience": audience,
        "client": client,
        "tags": [tag for tag in (clean(row.get("tags")).split(";") if clean(row.get("tags")) else []) if tag.strip()],
        "status": status,
        "verified": parse_csv_bool(row.get("verified"), "verified", errors),
        "email_validation": {
            "status": validation_status or EmailValidationStatus.UNKNOWN,
            "reason": clean(row.get("email_validation_reason")),
        },
        "suppression": {
            "global_unsubscribed": parse_csv_bool(row.get("global_unsubscribed"), "global_unsubscribed", errors),
            "hard_bounced": parse_csv_bool(row.get("hard_bounced"), "hard_bounced", errors),
            "complained": parse_csv_bool(row.get("complained"), "complained", errors),
        },
    }
    if clean(row.get("email_validated_at")):
        item["email_validation"]["validated_at"] = clean(row.get("email_validated_at"))
    if parse_csv_bool(row.get("unsubscribed"), "unsubscribed", errors):
        item["status"] = SubscriptionStatus.UNSUBSCRIBED
    return item, errors


def parse_csv_bool(value, field, errors):
    normalized = clean(value).casefold()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    errors[field] = "must_be_boolean"
    return False


def csv_import_contacts_for_client(csv_text, data, authenticated_client):
    contacts, row_errors = parse_csv_import(
        csv_text,
        audience_slug=clean(data.get("audience")),
        client_slug=clean(data.get("client")),
    )
    payload = {
        "audience": data.get("audience"),
        "client": data.get("client"),
        "dry_run": parse_csv_bool(data.get("dry_run"), "dry_run", {}) if data.get("dry_run") not in (None, "") else False,
        "idempotency_key": data.get("idempotency_key", ""),
        "contacts": contacts,
    }
    result = bulk_import_contacts_for_client(payload, authenticated_client)
    for error in row_errors:
        result["counts"]["total"] += 1
        result["counts"]["invalid"] += 1
        result["errors"].append(error)
    return result


def export_scope_from_data(data, authenticated_client):
    scope = validate_contact_scope(
        {"audience": data.get("audience"), "client": data.get("client"), "email": "scope@example.com"},
        authenticated_client,
    )
    return ExportScope(audience=scope.audience, client=scope.client)


def filtered_contact_queryset(data, authenticated_client):
    scope = export_scope_from_data(data, authenticated_client)
    queryset = (
        Contact.objects.filter(subscriptions__audience=scope.audience, subscriptions__client=scope.client)
        .select_related()
        .distinct()
        .order_by("id")
    )
    queryset = apply_export_filters(queryset, data, scope)
    return queryset, scope


def apply_export_filters(queryset, data, scope):
    tags = parse_filter_list(data.get("tags"))
    for tag in tags:
        queryset = queryset.filter(contact_tags__tag__audience=scope.audience, contact_tags__tag__slug=normalize_tag_slug(tag))

    subscription_status = clean(data.get("subscription_status"))
    if subscription_status:
        if subscription_status not in {choice.value for choice in SubscriptionStatus}:
            raise ApiValidationError({"subscription_status": "invalid"})
        queryset = queryset.filter(
            subscriptions__audience=scope.audience,
            subscriptions__client=scope.client,
            subscriptions__status=subscription_status,
        )

    if clean(data.get("verified")):
        verified = parse_query_bool(data.get("verified"), "verified")
        verified_filter = Q(verified_at__isnull=False) | Q(
            subscriptions__audience=scope.audience,
            subscriptions__client=scope.client,
            subscriptions__verified_at__isnull=False,
        )
        queryset = queryset.filter(verified_filter) if verified else queryset.exclude(verified_filter)

    validation_status = clean(data.get("email_validation_status"))
    if validation_status:
        if validation_status not in {choice.value for choice in EmailValidationStatus}:
            raise ApiValidationError({"email_validation_status": "invalid"})
        queryset = queryset.filter(email_validation_status=validation_status)

    suppression = clean(data.get("suppression"))
    if suppression:
        if suppression == "none":
            queryset = queryset.filter(
                global_unsubscribed_at__isnull=True,
                hard_bounced_at__isnull=True,
                complained_at__isnull=True,
            )
        elif suppression == "any":
            queryset = queryset.filter(
                Q(global_unsubscribed_at__isnull=False)
                | Q(hard_bounced_at__isnull=False)
                | Q(complained_at__isnull=False)
            )
        elif suppression == "global_unsubscribed":
            queryset = queryset.filter(global_unsubscribed_at__isnull=False)
        elif suppression == "hard_bounced":
            queryset = queryset.filter(hard_bounced_at__isnull=False)
        elif suppression == "complained":
            queryset = queryset.filter(complained_at__isnull=False)
        else:
            raise ApiValidationError({"suppression": "invalid"})

    updated_since = clean(data.get("updated_since"))
    if updated_since:
        parsed = parse_datetime(updated_since)
        if parsed is None:
            raise ApiValidationError({"updated_since": "must_be_iso_datetime"})
        queryset = queryset.filter(updated_at__gte=parsed)
    return queryset.distinct()


def parse_filter_list(value):
    if not value:
        return []
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value).split(",")
    return [item.strip() for item in raw if item.strip()]


def parse_query_bool(value, field):
    normalized = clean(value).casefold()
    if normalized in TRUE_VALUES - {""}:
        return True
    if normalized in FALSE_VALUES - {""}:
        return False
    raise ApiValidationError({field: "must_be_boolean"})


def validate_export_limit(value, *, default=50, maximum=500):
    if value in (None, ""):
        return default
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiValidationError({"limit": "must_be_integer"}) from exc
    if limit < 1 or limit > maximum:
        raise ApiValidationError({"limit": f"must_be_between_1_and_{maximum}"})
    return limit


def validate_export_cursor(value):
    if value in (None, ""):
        return None
    try:
        cursor = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiValidationError({"cursor": "must_be_integer"}) from exc
    if cursor < 1:
        raise ApiValidationError({"cursor": "must_be_positive"})
    return cursor


def export_contacts_for_client(data, authenticated_client):
    limit = validate_export_limit(data.get("limit"))
    cursor = validate_export_cursor(data.get("cursor") or data.get("offset"))
    queryset, scope = filtered_contact_queryset(data, authenticated_client)
    if cursor is not None:
        queryset = queryset.filter(id__gt=cursor)
    contacts = list(queryset[: limit + 1])
    next_cursor = None
    if len(contacts) > limit:
        next_cursor = contacts[limit - 1].id
        contacts = contacts[:limit]

    return {
        "audience": scope.audience.slug,
        "client": scope.client.slug,
        "count": len(contacts),
        "next_cursor": str(next_cursor) if next_cursor else None,
        "contacts": [export_contact_payload(contact, scope.audience, scope.client) for contact in contacts],
    }


def export_contact_payload(contact, audience, client):
    subscription = Subscription.objects.filter(contact=contact, audience=audience, client=client).first()
    payload = contact_payload(contact, audience, client, requested_email=contact.email)
    payload |= {
        "tags": contact_tags(contact, audience),
        "subscription_status": subscription.status if subscription else None,
        "unsubscribed": bool(subscription and subscription.status == SubscriptionStatus.UNSUBSCRIBED),
        "unsubscribed_at": isoformat(subscription.unsubscribed_at) if subscription else None,
        "unsubscribe_reason": subscription.unsubscribe_reason if subscription else "",
        "created_at": isoformat(contact.created_at),
        "updated_at": isoformat(contact.updated_at),
    }
    return payload


def export_contacts_csv_for_client(data, authenticated_client):
    queryset, scope = filtered_contact_queryset(data, authenticated_client)
    limit = validate_export_limit(data.get("limit"), default=10000, maximum=10000)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for contact in queryset[:limit]:
        writer.writerow(export_contact_csv_row(contact, scope.audience, scope.client))
    return output.getvalue()


def export_contact_csv_row(contact, audience, client):
    subscription = Subscription.objects.filter(contact=contact, audience=audience, client=client).first()
    return {
        "email": contact.normalized_email,
        "audience": audience.slug,
        "client": client.slug,
        "tags": ";".join(contact_tags(contact, audience)),
        "subscription_status": subscription.status if subscription else "",
        "verified": bool(contact.verified_at or (subscription and subscription.verified_at)),
        "verified_at": isoformat(contact.verified_at or (subscription.verified_at if subscription else None)) or "",
        "email_validation_status": contact.email_validation_status,
        "email_validation_reason": contact.email_validation_reason,
        "email_validated_at": isoformat(contact.email_validated_at) or "",
        "global_unsubscribed": bool(contact.global_unsubscribed_at),
        "hard_bounced": bool(contact.hard_bounced_at),
        "complained": bool(contact.complained_at),
        "unsubscribed": bool(subscription and subscription.status == SubscriptionStatus.UNSUBSCRIBED),
        "unsubscribed_at": isoformat(subscription.unsubscribed_at) if subscription and subscription.unsubscribed_at else "",
        "unsubscribe_reason": subscription.unsubscribe_reason if subscription else "",
        "updated_at": isoformat(contact.updated_at),
    }


def clean(value):
    return str(value or "").strip()
