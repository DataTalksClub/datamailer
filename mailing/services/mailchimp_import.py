import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mailing.models import (
    Audience,
    Client,
    Contact,
    ContactSourceMetadata,
    ContactTag,
    EmailValidationStatus,
    Subscription,
    SubscriptionStatus,
    Tag,
)
from mailing.services.contacts import normalize_email, normalize_tag_slug

MAILCHIMP_SOURCE = "mailchimp"
MAILCHIMP_CATEGORIES = ("subscribed", "unsubscribed", "cleaned")


class MailchimpImportError(ValueError):
    pass


@dataclass(frozen=True)
class MailchimpImportTarget:
    organization_slug: str
    audience_slug: str
    client_slug: str


@dataclass
class MailchimpImportReport:
    dry_run: bool
    target: dict
    archive: dict
    counts: dict = field(
        default_factory=lambda: {
            "files_seen": 0,
            "rows_seen": 0,
            "processed_rows": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "invalid_rows": 0,
            "skipped_rows": 0,
            "contacts_created": 0,
            "contacts_updated": 0,
            "subscriptions_created": 0,
            "subscriptions_updated": 0,
            "tags_created": 0,
            "tag_memberships_created": 0,
            "metadata_created": 0,
            "metadata_updated": 0,
            "subscribed_rows": 0,
            "unsubscribed_rows": 0,
            "cleaned_rows": 0,
        }
    )
    category_counts: dict = field(default_factory=lambda: {category: 0 for category in MAILCHIMP_CATEGORIES})
    invalid_rows: list[dict] = field(default_factory=list)
    row_results: list[dict] = field(default_factory=list)
    columns: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            "dry_run": self.dry_run,
            "target": self.target,
            "archive": self.archive,
            "counts": self.counts,
            "category_counts": self.category_counts,
            "columns": self.columns,
            "invalid_rows": self.invalid_rows,
            "row_results": self.row_results,
        }

    def to_json(self):
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


@dataclass(frozen=True)
class MailchimpRow:
    category: str
    archive_member: str
    row_number: int
    email: str
    normalized_email: str
    tags: list[str]
    metadata: dict
    event_time: object | None
    unsubscribe_reason: str


def import_mailchimp_zip(zip_path, target: MailchimpImportTarget, *, dry_run=False):
    audience, client = resolve_target(target)
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise MailchimpImportError(f"Mailchimp zip path does not exist: {zip_path}")
    if not zip_path.is_file():
        raise MailchimpImportError(f"Mailchimp zip path is not a file: {zip_path}")

    report = MailchimpImportReport(
        dry_run=dry_run,
        target={
            "organization": target.organization_slug,
            "audience": target.audience_slug,
            "client": target.client_slug,
        },
        archive={"path": str(zip_path), "categories": list(MAILCHIMP_CATEGORIES)},
    )

    with zipfile.ZipFile(zip_path) as archive:
        members = [member for member in archive.infolist() if not member.is_dir() and member.filename.endswith(".csv")]
        for member in members:
            category = category_for_filename(member.filename)
            if category is None:
                continue
            report.counts["files_seen"] += 1
            parse_member(archive, member, category, report, audience, client, dry_run=dry_run)

    return report


def resolve_target(target: MailchimpImportTarget):
    try:
        audience = Audience.objects.select_related("organization").get(
            organization__slug=target.organization_slug,
            slug=target.audience_slug,
        )
    except Audience.DoesNotExist as exc:
        raise MailchimpImportError(
            f"Audience '{target.audience_slug}' was not found in organization '{target.organization_slug}'."
        ) from exc

    try:
        client = Client.objects.get(organization=audience.organization, slug=target.client_slug)
    except Client.DoesNotExist as exc:
        raise MailchimpImportError(
            f"Client '{target.client_slug}' was not found in organization '{target.organization_slug}'."
        ) from exc

    return audience, client


def category_for_filename(filename):
    lowered = Path(filename).name.casefold()
    for category in MAILCHIMP_CATEGORIES:
        if lowered.startswith(f"{category}_") or f"/{category}_" in lowered:
            return category
    return None


def parse_member(archive, member, category, report, audience, client, *, dry_run):
    with archive.open(member) as raw_file:
        text_file = io.TextIOWrapper(raw_file, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text_file)
        if not reader.fieldnames:
            raise MailchimpImportError(f"Mailchimp CSV '{member.filename}' is missing a header row.")
        report.columns[member.filename] = list(reader.fieldnames)
        for row_number, row in enumerate(reader, start=2):
            report.counts["rows_seen"] += 1
            report.category_counts[category] += 1
            report.counts[f"{category}_rows"] += 1
            parsed, errors = parse_row(category, member.filename, row_number, row)
            if errors:
                report.counts["invalid_rows"] += 1
                report.counts["skipped_rows"] += 1
                report.invalid_rows.append(
                    {"file": member.filename, "row": row_number, "category": category, "errors": errors}
                )
                continue

            report.counts["processed_rows"] += 1
            if dry_run:
                report.counts["skipped_rows"] += 1
                report.row_results.append(
                    {
                        "file": member.filename,
                        "row": row_number,
                        "category": category,
                        "email": parsed.normalized_email,
                        "action": "would_import",
                    }
                )
                continue

            result = apply_mailchimp_row(parsed, audience, client)
            for count_name, value in result["counts"].items():
                report.counts[count_name] += value
            report.row_results.append(result["result"])


def parse_row(category, archive_member, row_number, row):
    errors = []
    email = clean(row.get("Email Address") or row.get("Email"))
    if not email:
        errors.append("email is required")
        normalized_email = ""
    else:
        normalized_email = normalize_email(email)
        try:
            validate_email(normalized_email)
        except ValidationError:
            errors.append("email is malformed")

    metadata = mailchimp_metadata(row, category)
    event_time = event_timestamp(row, category)
    return (
        MailchimpRow(
            category=category,
            archive_member=archive_member,
            row_number=row_number,
            email=email,
            normalized_email=normalized_email,
            tags=parse_tags(row.get("TAGS")),
            metadata=metadata,
            event_time=event_time,
            unsubscribe_reason=unsubscribe_reason(row, category),
        ),
        errors,
    )


def mailchimp_metadata(row, category):
    metadata = {
        "source": MAILCHIMP_SOURCE,
        "status": category,
        "mailchimp": {
            "leid": clean(row.get("LEID")),
            "euid": clean(row.get("EUID")),
            "member_rating": clean(row.get("MEMBER_RATING")),
            "notes": clean(row.get("NOTES")),
            "tags": parse_tags(row.get("TAGS")),
            "optin_time": clean(row.get("OPTIN_TIME")),
            "confirm_time": clean(row.get("CONFIRM_TIME")),
            "gmtoff": clean(row.get("GMTOFF")),
            "dstoff": clean(row.get("DSTOFF")),
            "timezone": clean(row.get("TIMEZONE")),
            "country": clean(row.get("CC")),
            "region": clean(row.get("REGION")),
            "last_changed": clean(row.get("LAST_CHANGED")),
        },
    }
    if clean(row.get("OPTIN_IP")):
        metadata["mailchimp"]["optin_ip"] = clean(row.get("OPTIN_IP"))
    if clean(row.get("CONFIRM_IP")):
        metadata["mailchimp"]["confirm_ip"] = clean(row.get("CONFIRM_IP"))
    if category == "unsubscribed":
        metadata["mailchimp"] |= {
            "unsub_time": clean(row.get("UNSUB_TIME")),
            "unsub_campaign_title": clean(row.get("UNSUB_CAMPAIGN_TITLE")),
            "unsub_campaign_id": clean(row.get("UNSUB_CAMPAIGN_ID")),
            "unsub_reason": clean(row.get("UNSUB_REASON")),
            "unsub_reason_other": clean(row.get("UNSUB_REASON_OTHER")),
        }
    if category == "cleaned":
        metadata["mailchimp"] |= {
            "clean_time": clean(row.get("CLEAN_TIME")),
            "clean_campaign_title": clean(row.get("CLEAN_CAMPAIGN_TITLE")),
            "clean_campaign_id": clean(row.get("CLEAN_CAMPAIGN_ID")),
        }
    return compact(metadata)


def event_timestamp(row, category):
    fields = {
        "subscribed": ("CONFIRM_TIME", "OPTIN_TIME", "LAST_CHANGED"),
        "unsubscribed": ("UNSUB_TIME", "LAST_CHANGED"),
        "cleaned": ("CLEAN_TIME", "LAST_CHANGED"),
    }[category]
    for field_name in fields:
        parsed = parse_mailchimp_datetime(row.get(field_name))
        if parsed is not None:
            return parsed
    return timezone.now()


def parse_mailchimp_datetime(value):
    value = clean(value)
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None and " " in value:
        parsed = parse_datetime(value.replace(" ", "T"))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone=timezone.get_current_timezone())
    return parsed


def unsubscribe_reason(row, category):
    if category != "unsubscribed":
        return ""
    reason = clean(row.get("UNSUB_REASON_OTHER")) or clean(row.get("UNSUB_REASON")) or "mailchimp_unsubscribed"
    campaign_id = clean(row.get("UNSUB_CAMPAIGN_ID"))
    if campaign_id:
        return f"{reason} ({campaign_id})"[:255]
    return reason[:255]


def parse_tags(value):
    raw = clean(value)
    if not raw:
        return []
    tags = []
    seen = set()
    for tag in raw.replace(",", ";").split(";"):
        tag = tag.strip()
        slug = normalize_tag_slug(tag)
        if not tag or not slug or slug in seen:
            continue
        seen.add(slug)
        tags.append(tag)
    return tags


@transaction.atomic
def apply_mailchimp_row(parsed: MailchimpRow, audience, client):
    counts = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "contacts_created": 0,
        "contacts_updated": 0,
        "subscriptions_created": 0,
        "subscriptions_updated": 0,
        "tags_created": 0,
        "tag_memberships_created": 0,
        "metadata_created": 0,
        "metadata_updated": 0,
    }
    before = snapshot(parsed.normalized_email, audience, client)

    contact, contact_created = Contact.objects.get_or_create(
        normalized_email=parsed.normalized_email,
        defaults={"email": parsed.email},
    )
    if contact_created:
        counts["contacts_created"] += 1

    contact_updates = []
    if contact.email != parsed.email:
        contact.email = parsed.email
        contact_updates.append("email")

    apply_contact_state(parsed, contact, contact_updates)
    if contact_updates:
        contact_updates.append("updated_at")
        contact.save(update_fields=sorted(set(contact_updates)))
        if not contact_created:
            counts["contacts_updated"] += 1

    subscription, subscription_created = Subscription.objects.get_or_create(
        contact=contact,
        audience=audience,
        client=client,
        defaults={"status": SubscriptionStatus.PENDING},
    )
    if subscription_created:
        counts["subscriptions_created"] += 1

    subscription_changed = apply_subscription_state(parsed, subscription)
    if subscription_changed:
        subscription.save()
        if not subscription_created:
            counts["subscriptions_updated"] += 1

    for tag_name in parsed.tags:
        tag_slug = normalize_tag_slug(tag_name)
        tag, tag_created = Tag.objects.get_or_create(
            audience=audience,
            slug=tag_slug,
            defaults={"name": tag_name[:120]},
        )
        _, membership_created = ContactTag.objects.get_or_create(contact=contact, tag=tag)
        if tag_created:
            counts["tags_created"] += 1
        if membership_created:
            counts["tag_memberships_created"] += 1

    metadata_changed, metadata_created = upsert_source_metadata(parsed, contact, audience, client)
    if metadata_created:
        counts["metadata_created"] += 1
    elif metadata_changed:
        counts["metadata_updated"] += 1

    after = snapshot(parsed.normalized_email, audience, client)
    action = "created" if before is None else "unchanged" if before == after else "updated"
    counts[action] += 1
    return {
        "counts": counts,
        "result": {
            "file": parsed.archive_member,
            "row": parsed.row_number,
            "category": parsed.category,
            "email": parsed.normalized_email,
            "action": action,
        },
    }


def apply_contact_state(parsed, contact, updates):
    event_time = parsed.event_time or timezone.now()
    if parsed.category == "subscribed":
        if contact.verified_at is None:
            contact.verified_at = event_time
            updates.append("verified_at")
        if contact.email_validation_status == EmailValidationStatus.UNKNOWN:
            contact.email_validation_status = EmailValidationStatus.EXTERNALLY_VALIDATED
            contact.email_validation_reason = "mailchimp_subscribed"
            contact.email_validated_at = event_time
            updates.extend(["email_validation_status", "email_validation_reason", "email_validated_at"])
    elif parsed.category == "unsubscribed":
        if contact.global_unsubscribed_at is None:
            contact.global_unsubscribed_at = event_time
            updates.append("global_unsubscribed_at")
    elif parsed.category == "cleaned":
        if contact.hard_bounced_at is None:
            contact.hard_bounced_at = event_time
            updates.append("hard_bounced_at")
        if contact.email_validation_status != EmailValidationStatus.MANUALLY_INVALID:
            contact.email_validation_status = EmailValidationStatus.MANUALLY_INVALID
            contact.email_validation_reason = "mailchimp_cleaned"
            contact.email_validated_at = event_time
            updates.extend(["email_validation_status", "email_validation_reason", "email_validated_at"])


def apply_subscription_state(parsed, subscription):
    event_time = parsed.event_time or timezone.now()
    changed = False
    if parsed.category == "subscribed":
        if subscription.status != SubscriptionStatus.UNSUBSCRIBED and subscription.status != SubscriptionStatus.SUBSCRIBED:
            subscription.status = SubscriptionStatus.SUBSCRIBED
            subscription.unsubscribed_at = None
            subscription.unsubscribe_reason = ""
            changed = True
        if subscription.verified_at is None:
            subscription.verified_at = event_time
            changed = True
    elif parsed.category == "unsubscribed":
        if subscription.status != SubscriptionStatus.UNSUBSCRIBED:
            subscription.status = SubscriptionStatus.UNSUBSCRIBED
            subscription.unsubscribed_at = subscription.unsubscribed_at or event_time
            subscription.unsubscribe_reason = parsed.unsubscribe_reason or "mailchimp_unsubscribed"
            changed = True
        elif not subscription.unsubscribe_reason and parsed.unsubscribe_reason:
            subscription.unsubscribe_reason = parsed.unsubscribe_reason
            changed = True
    elif parsed.category == "cleaned":
        if subscription.status != SubscriptionStatus.UNSUBSCRIBED:
            subscription.status = SubscriptionStatus.UNSUBSCRIBED
            subscription.unsubscribed_at = subscription.unsubscribed_at or event_time
            subscription.unsubscribe_reason = "mailchimp_cleaned"
            changed = True
    return changed


def upsert_source_metadata(parsed, contact, audience, client):
    external_id = parsed.metadata.get("mailchimp", {}).get("euid") or parsed.metadata.get("mailchimp", {}).get("leid", "")
    source_metadata, created = ContactSourceMetadata.objects.get_or_create(
        contact=contact,
        audience=audience,
        client=client,
        source=MAILCHIMP_SOURCE,
        defaults={"external_id": external_id, "metadata": parsed.metadata},
    )
    if created:
        return True, True

    if source_metadata.external_id != external_id or source_metadata.metadata != parsed.metadata:
        source_metadata.external_id = external_id
        source_metadata.metadata = parsed.metadata
        source_metadata.save(update_fields=["external_id", "metadata", "updated_at"])
        return True, False
    return False, False


def snapshot(normalized_email, audience, client):
    contact = Contact.objects.filter(normalized_email=normalized_email).first()
    if contact is None:
        return None
    subscription = Subscription.objects.filter(contact=contact, audience=audience, client=client).first()
    metadata = ContactSourceMetadata.objects.filter(
        contact=contact,
        audience=audience,
        client=client,
        source=MAILCHIMP_SOURCE,
    ).first()
    return {
        "email": contact.email,
        "verified_at": contact.verified_at,
        "email_validation_status": contact.email_validation_status,
        "email_validation_reason": contact.email_validation_reason,
        "email_validated_at": contact.email_validated_at,
        "global_unsubscribed_at": contact.global_unsubscribed_at,
        "hard_bounced_at": contact.hard_bounced_at,
        "subscription_status": subscription.status if subscription else None,
        "subscription_verified_at": subscription.verified_at if subscription else None,
        "unsubscribed_at": subscription.unsubscribed_at if subscription else None,
        "unsubscribe_reason": subscription.unsubscribe_reason if subscription else "",
        "tags": tuple(sorted(ContactTag.objects.filter(contact=contact, tag__audience=audience).values_list("tag__slug", flat=True))),
        "external_id": metadata.external_id if metadata else "",
        "metadata": metadata.metadata if metadata else {},
    }


def compact(value):
    if isinstance(value, dict):
        return {key: compact(child) for key, child in value.items() if compact(child) not in ("", None, [], {})}
    if isinstance(value, list):
        return [compact(item) for item in value if compact(item) not in ("", None, [], {})]
    return value


def clean(value):
    return str(value or "").strip()
