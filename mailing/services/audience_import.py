import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from mailing.models import Audience, Client, Contact, ContactTag, Subscription, SubscriptionStatus, Tag
from mailing.services.contacts import normalize_email

REQUIRED_COLUMNS = ("email",)
SUPPORTED_COLUMNS = (
    "email",
    "tags",
    "verified",
    "subscription_status",
    "unsubscribed",
    "global_unsubscribed",
    "hard_bounced",
    "complained",
    "suppressed",
    "unsubscribe_reason",
)

TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off", ""}
SUBSCRIPTION_VALUES = {
    "",
    SubscriptionStatus.PENDING,
    SubscriptionStatus.SUBSCRIBED,
    SubscriptionStatus.UNSUBSCRIBED,
}


@dataclass(frozen=True)
class ImportTarget:
    organization_slug: str
    audience_slug: str
    client_slug: str | None = None


@dataclass
class ParsedRow:
    row_number: int
    email: str
    normalized_email: str
    tags: list[str]
    verified: bool
    subscription_status: str
    unsubscribed: bool
    global_unsubscribed: bool
    hard_bounced: bool
    complained: bool
    suppressed: bool
    unsubscribe_reason: str

    @property
    def should_unsubscribe_subscription(self):
        return self.unsubscribed or self.subscription_status == SubscriptionStatus.UNSUBSCRIBED

    @property
    def should_create_subscription(self):
        return bool(self.subscription_status or self.verified or self.should_unsubscribe_subscription)


@dataclass
class ImportReport:
    dry_run: bool
    target: dict
    columns: dict
    counts: dict = field(
        default_factory=lambda: {
            "rows_seen": 0,
            "valid_rows": 0,
            "processed_rows": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "duplicate_input_rows": 0,
            "invalid_rows": 0,
            "skipped_rows": 0,
            "contacts_created": 0,
            "contacts_updated": 0,
            "subscriptions_created": 0,
            "subscriptions_updated": 0,
            "tags_created": 0,
            "tag_memberships_created": 0,
            "verified_applied": 0,
            "unsubscribed_applied": 0,
            "global_unsubscribed_applied": 0,
            "hard_bounced_applied": 0,
            "complained_applied": 0,
            "suppressed_applied": 0,
        }
    )
    invalid_rows: list[dict] = field(default_factory=list)
    duplicate_rows: list[dict] = field(default_factory=list)
    row_results: list[dict] = field(default_factory=list)

    def as_dict(self):
        return {
            "dry_run": self.dry_run,
            "target": self.target,
            "columns": self.columns,
            "counts": self.counts,
            "invalid_rows": self.invalid_rows,
            "duplicate_rows": self.duplicate_rows,
            "row_results": self.row_results,
        }

    def to_json(self):
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


class AudienceImportError(ValueError):
    pass


def import_audience_csv(csv_path, target: ImportTarget, *, dry_run=False):
    audience, client = resolve_target(target)
    report = ImportReport(
        dry_run=dry_run,
        target={
            "organization": target.organization_slug,
            "audience": target.audience_slug,
            "client": target.client_slug,
        },
        columns={
            "required": list(REQUIRED_COLUMNS),
            "supported": list(SUPPORTED_COLUMNS),
            "tag_separator": ";",
            "duplicate_precedence": "first valid row for a normalized email wins; later duplicates are skipped",
            "suppressed_mapping": "suppressed=true is imported as a global marketing suppression",
        },
    )
    seen_emails = {}

    with Path(csv_path).open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        validate_header(reader.fieldnames)
        for row_number, raw_row in enumerate(reader, start=2):
            report.counts["rows_seen"] += 1
            parsed, errors = parse_row(row_number, raw_row)
            if errors:
                report.counts["invalid_rows"] += 1
                report.counts["skipped_rows"] += 1
                report.invalid_rows.append({"row": row_number, "errors": errors})
                continue

            report.counts["valid_rows"] += 1
            if parsed.normalized_email in seen_emails:
                report.counts["duplicate_input_rows"] += 1
                report.counts["skipped_rows"] += 1
                report.duplicate_rows.append(
                    {
                        "row": row_number,
                        "email": parsed.email,
                        "normalized_email": parsed.normalized_email,
                        "kept_row": seen_emails[parsed.normalized_email],
                        "action": "skipped",
                    }
                )
                continue

            seen_emails[parsed.normalized_email] = row_number
            report.counts["processed_rows"] += 1
            if dry_run:
                report.row_results.append(
                    {
                        "row": row_number,
                        "email": parsed.normalized_email,
                        "action": "would_import",
                        "tags": [slugify(tag) for tag in parsed.tags],
                        "subscription_status": parsed.subscription_status or SubscriptionStatus.PENDING,
                    }
                )
                continue

            row_result = apply_row(parsed, audience, client)
            for count_name, value in row_result["counts"].items():
                report.counts[count_name] += value
            report.row_results.append(row_result["result"])

    return report


def resolve_target(target):
    try:
        audience = Audience.objects.select_related("organization").get(
            organization__slug=target.organization_slug,
            slug=target.audience_slug,
        )
    except Audience.DoesNotExist as exc:
        raise AudienceImportError(
            f"Audience '{target.audience_slug}' was not found in organization '{target.organization_slug}'."
        ) from exc

    client = None
    if target.client_slug:
        try:
            client = Client.objects.get(organization=audience.organization, slug=target.client_slug)
        except Client.DoesNotExist as exc:
            raise AudienceImportError(
                f"Client '{target.client_slug}' was not found in organization '{target.organization_slug}'."
            ) from exc

    return audience, client


def validate_header(fieldnames):
    if not fieldnames:
        raise AudienceImportError("CSV file is empty or missing a header row.")

    normalized = {name.strip() for name in fieldnames if name}
    missing = [column for column in REQUIRED_COLUMNS if column not in normalized]
    if missing:
        raise AudienceImportError(f"CSV is missing required column(s): {', '.join(missing)}.")

    unknown = sorted(normalized - set(SUPPORTED_COLUMNS))
    if unknown:
        raise AudienceImportError(f"CSV contains unsupported column(s): {', '.join(unknown)}.")


def parse_row(row_number, raw_row):
    errors = []
    email = clean(raw_row.get("email"))
    if not email:
        errors.append("email is required")
        normalized_email = ""
    else:
        normalized_email = normalize_email(email)
        try:
            validate_email(normalized_email)
        except ValidationError:
            errors.append("email is malformed")

    verified = parse_bool(raw_row.get("verified"), "verified", errors)
    global_unsubscribed = parse_bool(raw_row.get("global_unsubscribed"), "global_unsubscribed", errors)
    hard_bounced = parse_bool(raw_row.get("hard_bounced"), "hard_bounced", errors)
    complained = parse_bool(raw_row.get("complained"), "complained", errors)
    suppressed = parse_bool(raw_row.get("suppressed"), "suppressed", errors)
    unsubscribed = parse_bool(raw_row.get("unsubscribed"), "unsubscribed", errors)

    subscription_status = clean(raw_row.get("subscription_status")).casefold()
    if unsubscribed:
        subscription_status = SubscriptionStatus.UNSUBSCRIBED
    if subscription_status not in SUBSCRIPTION_VALUES:
        errors.append("subscription_status must be blank, pending, subscribed, or unsubscribed")

    tags = parse_tags(raw_row.get("tags"), errors)
    return (
        ParsedRow(
            row_number=row_number,
            email=email,
            normalized_email=normalized_email,
            tags=tags,
            verified=verified,
            subscription_status=subscription_status,
            unsubscribed=unsubscribed,
            global_unsubscribed=global_unsubscribed,
            hard_bounced=hard_bounced,
            complained=complained,
            suppressed=suppressed,
            unsubscribe_reason=clean(raw_row.get("unsubscribe_reason")),
        ),
        errors,
    )


def parse_bool(value, field_name, errors):
    normalized = clean(value).casefold()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    errors.append(f"{field_name} must be a boolean value")
    return False


def parse_tags(value, errors):
    raw_tags = clean(value)
    if not raw_tags:
        return []

    tags = []
    seen_slugs = set()
    for raw_tag in raw_tags.split(";"):
        tag = raw_tag.strip()
        tag_slug = slugify(tag)
        if not tag or not tag_slug:
            errors.append("tags must contain non-empty slug-safe values")
            continue
        if len(tag) > 120 or len(tag_slug) > 120:
            errors.append("tags must be 120 characters or fewer after slugging")
            continue
        if tag_slug in seen_slugs:
            continue
        seen_slugs.add(tag_slug)
        tags.append(tag)
    return tags


def clean(value):
    return str(value or "").strip()


@transaction.atomic
def apply_row(parsed: ParsedRow, audience, client):
    now = timezone.now()
    counts = {name: 0 for name in ImportReport(dry_run=False, target={}, columns={}).counts}
    contact, contact_created = Contact.objects.get_or_create(
        normalized_email=parsed.normalized_email,
        defaults={"email": parsed.email},
    )
    changed = False
    result = {
        "row": parsed.row_number,
        "email": parsed.normalized_email,
        "action": "unchanged",
        "tags": [],
        "subscription_status": None,
        "preserved_existing_opt_out": False,
    }

    if contact_created:
        counts["contacts_created"] += 1
        changed = True
    elif contact.email != parsed.email:
        contact.email = parsed.email
        changed = True

    contact_updates = []
    for field_name, should_apply, count_name in (
        ("verified_at", parsed.verified, "verified_applied"),
        ("global_unsubscribed_at", parsed.global_unsubscribed or parsed.suppressed, "global_unsubscribed_applied"),
        ("hard_bounced_at", parsed.hard_bounced, "hard_bounced_applied"),
        ("complained_at", parsed.complained, "complained_applied"),
    ):
        if should_apply and getattr(contact, field_name) is None:
            setattr(contact, field_name, now)
            contact_updates.append(field_name)
            counts[count_name] += 1
            changed = True
    if parsed.suppressed and "global_unsubscribed_at" in contact_updates:
        counts["suppressed_applied"] += 1

    if contact_updates or (not contact_created and contact.email == parsed.email and changed):
        contact_updates.extend(["email", "updated_at"])
        contact.save(update_fields=sorted(set(contact_updates)))
        counts["contacts_updated"] += 0 if contact_created else 1

    subscription_changed, subscription_created, subscription_status, preserved_opt_out = apply_subscription(
        parsed,
        contact,
        audience,
        client,
        now,
    )
    if subscription_created:
        counts["subscriptions_created"] += 1
        changed = True
    elif subscription_changed:
        counts["subscriptions_updated"] += 1
        changed = True

    if parsed.should_unsubscribe_subscription and subscription_changed:
        counts["unsubscribed_applied"] += 1

    for tag_name in parsed.tags:
        tag_slug = slugify(tag_name)
        tag, tag_created = Tag.objects.get_or_create(
            audience=audience,
            slug=tag_slug,
            defaults={"name": tag_name},
        )
        membership, membership_created = ContactTag.objects.get_or_create(contact=contact, tag=tag)
        if tag_created:
            counts["tags_created"] += 1
            changed = True
        if membership_created:
            counts["tag_memberships_created"] += 1
            changed = True
        result["tags"].append(tag.slug)

    if contact_created:
        counts["created"] += 1
        result["action"] = "created"
    elif changed:
        counts["updated"] += 1
        result["action"] = "updated"
    else:
        counts["unchanged"] += 1

    result["subscription_status"] = subscription_status
    result["preserved_existing_opt_out"] = preserved_opt_out
    return {"counts": counts, "result": result}


def apply_subscription(parsed: ParsedRow, contact, audience, client, now):
    if not parsed.should_create_subscription:
        return False, False, None, False

    subscription, created = Subscription.objects.get_or_create(
        contact=contact,
        audience=audience,
        client=client,
        defaults={"status": SubscriptionStatus.PENDING},
    )
    changed = created
    preserved_opt_out = False
    target_status = parsed.subscription_status or SubscriptionStatus.PENDING

    if parsed.should_unsubscribe_subscription:
        if subscription.status != SubscriptionStatus.UNSUBSCRIBED:
            subscription.status = SubscriptionStatus.UNSUBSCRIBED
            subscription.unsubscribed_at = subscription.unsubscribed_at or now
            subscription.unsubscribe_reason = parsed.unsubscribe_reason or "csv_import"
            changed = True
        elif not subscription.unsubscribe_reason and parsed.unsubscribe_reason:
            subscription.unsubscribe_reason = parsed.unsubscribe_reason
            changed = True
    elif subscription.status == SubscriptionStatus.UNSUBSCRIBED:
        preserved_opt_out = target_status == SubscriptionStatus.SUBSCRIBED
    elif target_status and subscription.status != target_status:
        subscription.status = target_status
        changed = True

    if parsed.verified and subscription.verified_at is None:
        subscription.verified_at = now
        changed = True

    if changed:
        subscription.save()
    return changed, created, subscription.status, preserved_opt_out
