import hashlib
import json
from json import JSONDecodeError
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from mailing.models import (
    RecipientList,
    RecipientListImportJob,
    RecipientListImportJobStatus,
    RecipientListMember,
    RecipientListType,
)
from mailing.services.api import ApiValidationError, isoformat, validate_contact_scope
from mailing.services.contacts import normalize_email, upsert_contact

ROOT_LIST_KEY = "<all>"
CASCADE_SOURCE_PREFIX = "cascade-contact:"
DEFAULT_IMPORT_FETCH_TIMEOUT = 30
DEFAULT_IMPORT_MAX_BYTES = 50 * 1024 * 1024


def validate_path_key(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ApiValidationError({field: "required"})
    key = value.strip()
    if "/" in key or len(key) > 255:
        raise ApiValidationError({field: "invalid"})
    return key


def validate_metadata(value, field):
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ApiValidationError({field: "must_be_object"})
    return value


def validate_list_type(value):
    if value in (None, ""):
        return RecipientListType.CUSTOM
    valid_types = {choice.value for choice in RecipientListType}
    if value not in valid_types:
        raise ApiValidationError({"type": "invalid"})
    return value


def validate_member_status(value):
    if value in (None, ""):
        return True
    if value == "active":
        return True
    if value == "removed":
        return False
    raise ApiValidationError({"member.status": "invalid"})


def recipient_list_payload(recipient_list):
    return {
        "key": recipient_list.key,
        "type": recipient_list.type,
        "name": recipient_list.name,
        "audience": recipient_list.audience.slug,
        "client": recipient_list.client.slug,
        "metadata": recipient_list.metadata,
        "member_count": recipient_list.member_count,
        "active_member_count": recipient_list.active_member_count,
        "last_reconciled_at": isoformat(recipient_list.last_reconciled_at),
        "created_at": isoformat(recipient_list.created_at),
        "updated_at": isoformat(recipient_list.updated_at),
    }


def recipient_list_preview_payload(scope, list_key, defaults):
    return {
        "key": list_key,
        "type": defaults["type"],
        "name": defaults["name"],
        "audience": scope.audience.slug,
        "client": scope.client.slug,
        "metadata": defaults["metadata"],
        "member_count": 0,
        "active_member_count": 0,
        "last_reconciled_at": None,
        "created_at": None,
        "updated_at": None,
    }


def recipient_list_member_payload(member):
    return {
        "source_object_key": member.source_object_key,
        "email": member.email,
        "contact_id": member.contact_id,
        "status": "active" if member.active else "removed",
        "active": member.active,
        "removed_at": isoformat(member.removed_at),
        "metadata": member.metadata,
        "created_at": isoformat(member.created_at),
        "updated_at": isoformat(member.updated_at),
    }


def removed_recipient_list_member_preview_payload(source_object_key):
    return {
        "source_object_key": source_object_key,
        "email": "",
        "contact_id": None,
        "status": "removed",
        "active": False,
        "removed_at": None,
        "metadata": {},
        "created_at": None,
        "updated_at": None,
    }


def recipient_list_import_job_payload(job):
    return {
        "id": job.id,
        "list_key": job.list_key,
        "audience": job.audience.slug,
        "client": job.client.slug,
        "source_url": job.source_url,
        "idempotency_key": job.idempotency_key,
        "status": job.status,
        "list": job.list_defaults,
        "remove_absent": job.remove_absent,
        "row_count": job.row_count,
        "created_count": job.created_count,
        "updated_count": job.updated_count,
        "removed_count": job.removed_count,
        "failed_count": job.failed_count,
        "failed_rows": job.failed_rows,
        "content_sha256": job.content_sha256,
        "error": job.error,
        "started_at": isoformat(job.started_at),
        "completed_at": isoformat(job.completed_at),
        "created_at": isoformat(job.created_at),
        "updated_at": isoformat(job.updated_at),
        "recipient_list": recipient_list_payload(job.recipient_list) if job.recipient_list_id else None,
    }


def validate_recipient_list_scope(data, authenticated_client):
    return validate_contact_scope(data, authenticated_client, require_email=False)


def validate_list_defaults(data, list_key):
    list_data = data.get("list", data)
    if not isinstance(list_data, dict):
        raise ApiValidationError({"list": "must_be_object"})

    list_type = validate_list_type(list_data.get("type"))
    name = list_data.get("name", list_key)
    if not isinstance(name, str) or not name.strip():
        raise ApiValidationError({"name": "required"})

    return {
        "type": list_type,
        "name": name.strip(),
        "metadata": validate_metadata(list_data.get("metadata"), "metadata"),
    }


def validate_member_payload(data):
    member_data = data.get("member", data)
    if not isinstance(member_data, dict):
        raise ApiValidationError({"member": "must_be_object"})

    email = member_data.get("email")
    if not isinstance(email, str) or not email.strip():
        raise ApiValidationError({"member.email": "required"})
    try:
        validate_email(email.strip())
    except ValidationError as exc:
        raise ApiValidationError({"member.email": "invalid"}) from exc

    return {
        "email": email.strip(),
        "active": validate_member_status(member_data.get("status")),
        "metadata": validate_metadata(member_data.get("metadata"), "member.metadata"),
    }


def validate_import_url(value):
    if not isinstance(value, str) or not value.strip():
        raise ApiValidationError({"source_url": "required"})
    source_url = value.strip()
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ApiValidationError({"source_url": "invalid"})
    if len(source_url) > 2048:
        raise ApiValidationError({"source_url": "too_long"})
    return source_url


def validate_optional_string(value, field, *, max_length=255):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ApiValidationError({field: "must_be_string"})
    normalized = value.strip()
    if not normalized:
        return ""
    if len(normalized) > max_length:
        raise ApiValidationError({field: "too_long"})
    return normalized


def refresh_recipient_list_counts(recipient_list):
    counts = {
        "member_count": recipient_list.members.count(),
        "active_member_count": recipient_list.members.filter(active=True).count(),
    }
    RecipientList.objects.filter(pk=recipient_list.pk).update(**counts, updated_at=timezone.now())
    for field, value in counts.items():
        setattr(recipient_list, field, value)
    return recipient_list


def ancestor_list_keys(list_key):
    if list_key == ROOT_LIST_KEY:
        return []
    segments = list_key.split(":")
    ancestors = [":".join(segments[:index]) for index in range(len(segments) - 1, 0, -1)]
    ancestors.append(ROOT_LIST_KEY)
    return ancestors


def cascade_reason_for_member(recipient_list, member):
    return {
        "list_key": recipient_list.key,
        "source_object_key": member.source_object_key,
    }


def cascade_reasons(metadata):
    reasons = metadata.get("membership_reasons", []) if isinstance(metadata, dict) else []
    if not isinstance(reasons, list):
        return []
    return [
        reason
        for reason in reasons
        if isinstance(reason, dict)
        and isinstance(reason.get("list_key"), str)
        and isinstance(reason.get("source_object_key"), str)
    ]


def ancestor_list_defaults(list_key):
    return {
        "type": RecipientListType.CUSTOM,
        "name": ROOT_LIST_KEY if list_key == ROOT_LIST_KEY else list_key,
        "metadata": {"tree_node": True},
    }


def get_or_create_ancestor_list(scope_list, list_key):
    recipient_list, _ = RecipientList.objects.get_or_create(
        client=scope_list.client,
        audience=scope_list.audience,
        key=list_key,
        defaults=ancestor_list_defaults(list_key),
    )
    return recipient_list


def upsert_cascade_memberships(recipient_list, member):
    reason = cascade_reason_for_member(recipient_list, member)
    for ancestor_key in ancestor_list_keys(recipient_list.key):
        if member.active:
            ancestor = get_or_create_ancestor_list(recipient_list, ancestor_key)
        else:
            ancestor = RecipientList.objects.filter(
                client=recipient_list.client,
                audience=recipient_list.audience,
                key=ancestor_key,
            ).first()
            if ancestor is None:
                continue
        ancestor_member = RecipientListMember.objects.filter(
            recipient_list=ancestor,
            contact=member.contact,
        ).first()
        if ancestor_member is None:
            if not member.active:
                refresh_recipient_list_counts(ancestor)
                continue
            RecipientListMember.objects.create(
                recipient_list=ancestor,
                contact=member.contact,
                email=member.email,
                source_object_key=f"{CASCADE_SOURCE_PREFIX}{member.contact_id}",
                metadata={"membership_reasons": [reason]},
                active=True,
                removed_at=None,
            )
            refresh_recipient_list_counts(ancestor)
            continue

        metadata = ancestor_member.metadata if isinstance(ancestor_member.metadata, dict) else {}
        reasons = [
            existing
            for existing in cascade_reasons(metadata)
            if existing != reason
        ]
        if member.active:
            reasons.append(reason)

        metadata = metadata | {"membership_reasons": reasons}
        updates = {
            "email": member.email,
            "metadata": metadata,
        }
        if reasons:
            updates["active"] = True
            updates["removed_at"] = None
        elif ancestor_member.source_object_key.startswith(CASCADE_SOURCE_PREFIX):
            updates["active"] = False
            updates["removed_at"] = timezone.now()

        for field, value in updates.items():
            setattr(ancestor_member, field, value)
        ancestor_member.save(update_fields=[*updates.keys(), "updated_at"])
        refresh_recipient_list_counts(ancestor)


@transaction.atomic
def upsert_recipient_list_for_client(list_key, data, authenticated_client):
    list_key = validate_path_key(list_key, "list_key")
    scope = validate_recipient_list_scope(data, authenticated_client)
    defaults = validate_list_defaults(data, list_key)

    recipient_list, created = RecipientList.objects.update_or_create(
        client=scope.client,
        audience=scope.audience,
        key=list_key,
        defaults=defaults,
    )
    refresh_recipient_list_counts(recipient_list)
    return {
        "recipient_list": recipient_list_payload(recipient_list),
        "created": created,
    }


def get_recipient_list_for_client(list_key, data, authenticated_client):
    list_key = validate_path_key(list_key, "list_key")
    scope = validate_recipient_list_scope(data, authenticated_client)
    recipient_list = RecipientList.objects.filter(
        client=scope.client,
        audience=scope.audience,
        key=list_key,
    ).first()
    if recipient_list is None:
        raise ApiValidationError({"list_key": "not_found"}, status_code=404)
    return {"recipient_list": recipient_list_payload(recipient_list)}


def get_recipient_list_members_for_client(list_key, data, authenticated_client):
    list_key = validate_path_key(list_key, "list_key")
    scope = validate_recipient_list_scope(data, authenticated_client)
    recipient_list = RecipientList.objects.filter(
        client=scope.client,
        audience=scope.audience,
        key=list_key,
    ).first()
    if recipient_list is None:
        raise ApiValidationError({"list_key": "not_found"}, status_code=404)

    include_removed = data.get("include_removed", "false")
    if include_removed in ("", None, "false", "0", "no"):
        include_removed = False
    elif include_removed in ("true", "1", "yes"):
        include_removed = True
    else:
        raise ApiValidationError({"include_removed": "must_be_boolean"})

    limit = data.get("limit", 1000)
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ApiValidationError({"limit": "must_be_integer"}) from exc
    if limit <= 0 or limit > 10000:
        raise ApiValidationError({"limit": "must_be_between_1_and_10000"})

    queryset = (
        recipient_list.members.select_related("contact")
        .order_by("source_object_key", "id")
    )
    if not include_removed:
        queryset = queryset.filter(active=True)

    members = list(queryset[: limit + 1])
    has_more = len(members) > limit
    members = members[:limit]
    return {
        "recipient_list": recipient_list_payload(recipient_list),
        "members": [recipient_list_member_payload(member) for member in members],
        "count": len(members),
        "has_more": has_more,
    }


def upsert_member(recipient_list, source_object_key, member_data):
    contact, _ = upsert_contact(member_data["email"])
    existing_by_source = RecipientListMember.objects.filter(
        recipient_list=recipient_list,
        source_object_key=source_object_key,
    ).first()
    existing_by_contact = RecipientListMember.objects.filter(
        recipient_list=recipient_list,
        contact=contact,
    ).first()

    if existing_by_source and existing_by_contact and existing_by_source.pk != existing_by_contact.pk:
        existing_by_source.delete()
        member = existing_by_contact
    else:
        member = existing_by_source or existing_by_contact

    existing_metadata = member.metadata if member and isinstance(member.metadata, dict) else {}
    existing_cascade_reasons = cascade_reasons(existing_metadata)
    active = member_data["active"] or bool(existing_cascade_reasons)
    removed_at = None
    if not active:
        removed_at = member.removed_at if member and member.removed_at else timezone.now()
    metadata = member_data["metadata"]
    if existing_cascade_reasons:
        metadata = metadata | {"membership_reasons": existing_cascade_reasons}
    defaults = {
        "contact": contact,
        "email": normalize_email(member_data["email"]),
        "source_object_key": source_object_key,
        "metadata": metadata,
        "active": active,
        "removed_at": removed_at,
    }

    if member is None:
        member = RecipientListMember.objects.create(recipient_list=recipient_list, **defaults)
        return member, True

    for field, value in defaults.items():
        setattr(member, field, value)
    member.save(update_fields=[*defaults.keys(), "updated_at"])
    return member, False


@transaction.atomic
def upsert_recipient_list_member_for_client(list_key, source_object_key, data, authenticated_client):
    list_key = validate_path_key(list_key, "list_key")
    source_object_key = validate_path_key(source_object_key, "source_object_key")
    list_result = upsert_recipient_list_for_client(list_key, data, authenticated_client)
    recipient_list = RecipientList.objects.get(
        client=authenticated_client,
        audience__slug=list_result["recipient_list"]["audience"],
        key=list_key,
    )
    member_data = validate_member_payload(data)

    member, created = upsert_member(recipient_list, source_object_key, member_data)
    refresh_recipient_list_counts(recipient_list)
    upsert_cascade_memberships(recipient_list, member)
    return {
        "recipient_list": recipient_list_payload(recipient_list),
        "member": recipient_list_member_payload(member),
        "created": created,
    }


@transaction.atomic
def remove_recipient_list_member_for_client(list_key, source_object_key, data, authenticated_client):
    list_key = validate_path_key(list_key, "list_key")
    source_object_key = validate_path_key(source_object_key, "source_object_key")
    scope = validate_recipient_list_scope(data, authenticated_client)
    defaults = validate_list_defaults(data, list_key)
    recipient_list = RecipientList.objects.filter(
        client=scope.client,
        audience=scope.audience,
        key=list_key,
    ).first()
    if recipient_list is None:
        return {
            "recipient_list": recipient_list_preview_payload(scope, list_key, defaults),
            "member": removed_recipient_list_member_preview_payload(source_object_key),
            "removed": False,
        }

    member = recipient_list.members.filter(source_object_key=source_object_key).first()
    if member is None:
        refresh_recipient_list_counts(recipient_list)
        return {
            "recipient_list": recipient_list_payload(recipient_list),
            "member": removed_recipient_list_member_preview_payload(source_object_key),
            "removed": False,
        }

    was_active = member.active
    if member.active:
        reasons = cascade_reasons(member.metadata)
        if reasons:
            member.metadata = {"membership_reasons": reasons}
            member.removed_at = None
        else:
            member.active = False
            member.removed_at = timezone.now()
        member.save(update_fields=["active", "removed_at", "metadata", "updated_at"])
        upsert_cascade_memberships(recipient_list, member)
    refresh_recipient_list_counts(recipient_list)
    member.refresh_from_db()
    return {
        "recipient_list": recipient_list_payload(recipient_list),
        "member": recipient_list_member_payload(member),
        "removed": was_active,
    }


@transaction.atomic
def bulk_upsert_recipient_list_members_for_client(list_key, data, authenticated_client):
    list_key = validate_path_key(list_key, "list_key")
    members = data.get("members")
    if not isinstance(members, list):
        raise ApiValidationError({"members": "must_be_list"})

    list_result = upsert_recipient_list_for_client(list_key, data, authenticated_client)
    recipient_list = RecipientList.objects.get(
        client=authenticated_client,
        audience__slug=list_result["recipient_list"]["audience"],
        key=list_key,
    )

    created_count = 0
    updated_count = 0
    for index, raw_member in enumerate(members):
        if not isinstance(raw_member, dict):
            raise ApiValidationError({f"members.{index}": "must_be_object"})
        source_object_key = validate_path_key(raw_member.get("source_object_key"), f"members.{index}.source_object_key")
        member, created = upsert_member(recipient_list, source_object_key, validate_member_payload(raw_member))
        upsert_cascade_memberships(recipient_list, member)
        if created:
            created_count += 1
        else:
            updated_count += 1

    refresh_recipient_list_counts(recipient_list)
    return {
        "recipient_list": recipient_list_payload(recipient_list),
        "created_count": created_count,
        "updated_count": updated_count,
    }


@transaction.atomic
def reconcile_recipient_list_for_client(list_key, data, authenticated_client):
    list_key = validate_path_key(list_key, "list_key")
    scope = validate_recipient_list_scope(data, authenticated_client)
    defaults = validate_list_defaults(data, list_key)
    dry_run = data.get("dry_run", False)
    remove_absent = data.get("remove_absent", True)
    if not isinstance(dry_run, bool):
        raise ApiValidationError({"dry_run": "must_be_boolean"})
    if not isinstance(remove_absent, bool):
        raise ApiValidationError({"remove_absent": "must_be_boolean"})

    members = data.get("members")
    if not isinstance(members, list):
        raise ApiValidationError({"members": "must_be_list"})

    recipient_list = RecipientList.objects.filter(
        client=scope.client,
        audience=scope.audience,
        key=list_key,
    ).first()

    parsed_members = []
    incoming_keys = set()
    for index, raw_member in enumerate(members):
        if not isinstance(raw_member, dict):
            raise ApiValidationError({f"members.{index}": "must_be_object"})
        source_object_key = validate_path_key(raw_member.get("source_object_key"), f"members.{index}.source_object_key")
        incoming_keys.add(source_object_key)
        parsed_members.append((source_object_key, validate_member_payload(raw_member)))

    absent = RecipientListMember.objects.none()
    if recipient_list is not None:
        absent = recipient_list.members.filter(active=True).exclude(source_object_key__in=incoming_keys)
    absent_count = absent.count() if remove_absent else 0
    if dry_run:
        return {
            "recipient_list": (
                recipient_list_payload(recipient_list)
                if recipient_list is not None
                else recipient_list_preview_payload(scope, list_key, defaults)
            ),
            "dry_run": True,
            "upsert_count": len(parsed_members),
            "removed_count": absent_count,
        }

    recipient_list, _ = RecipientList.objects.update_or_create(
        client=scope.client,
        audience=scope.audience,
        key=list_key,
        defaults=defaults,
    )
    for source_object_key, member_data in parsed_members:
        member, _ = upsert_member(recipient_list, source_object_key, member_data)
        upsert_cascade_memberships(recipient_list, member)

    removed_count = 0
    if remove_absent:
        absent = list(
            recipient_list.members.select_related("contact")
            .filter(active=True)
            .exclude(source_object_key__in=incoming_keys)
        )
        removed_at = timezone.now()
        for member in absent:
            member.active = False
            member.removed_at = removed_at
            member.save(update_fields=["active", "removed_at", "updated_at"])
            upsert_cascade_memberships(recipient_list, member)
        removed_count = len(absent)

    recipient_list.last_reconciled_at = timezone.now()
    recipient_list.save(update_fields=["last_reconciled_at", "updated_at"])
    refresh_recipient_list_counts(recipient_list)
    return {
        "recipient_list": recipient_list_payload(recipient_list),
        "dry_run": False,
        "upsert_count": len(parsed_members),
        "removed_count": removed_count,
    }


@transaction.atomic
def create_recipient_list_import_job_for_client(list_key, data, authenticated_client):
    list_key = validate_path_key(list_key, "list_key")
    scope = validate_recipient_list_scope(data, authenticated_client)
    defaults = validate_list_defaults(data, list_key)
    source_url = validate_import_url(data.get("source_url") or data.get("url"))
    idempotency_key = validate_optional_string(data.get("idempotency_key"), "idempotency_key")
    remove_absent = data.get("remove_absent", False)
    if not isinstance(remove_absent, bool):
        raise ApiValidationError({"remove_absent": "must_be_boolean"})

    if idempotency_key:
        existing = RecipientListImportJob.objects.select_for_update().filter(
            client=scope.client,
            idempotency_key=idempotency_key,
        ).first()
        if existing is not None:
            return {"import_job": recipient_list_import_job_payload(existing), "created": False}

    job = RecipientListImportJob.objects.create(
        client=scope.client,
        audience=scope.audience,
        list_key=list_key,
        source_url=source_url,
        idempotency_key=idempotency_key,
        list_defaults=defaults,
        remove_absent=remove_absent,
    )
    return {"import_job": recipient_list_import_job_payload(job), "created": True}


def get_recipient_list_import_job_for_client(list_key, job_id, data, authenticated_client):
    list_key = validate_path_key(list_key, "list_key")
    scope = validate_recipient_list_scope(data, authenticated_client)
    job = RecipientListImportJob.objects.select_related("recipient_list", "audience", "client").filter(
        id=job_id,
        client=scope.client,
        audience=scope.audience,
        list_key=list_key,
    ).first()
    if job is None:
        raise ApiValidationError({"import_job": "not_found"}, status_code=404)
    return {"import_job": recipient_list_import_job_payload(job)}


def process_pending_recipient_list_import_jobs(*, limit=25):
    processed = 0
    succeeded = 0
    failed = 0
    job_ids = list(
        RecipientListImportJob.objects.filter(status=RecipientListImportJobStatus.PENDING)
        .order_by("created_at", "id")
        .values_list("id", flat=True)[:limit]
    )
    for job_id in job_ids:
        job = process_recipient_list_import_job(job_id)
        processed += 1
        if job.status == RecipientListImportJobStatus.SUCCEEDED:
            succeeded += 1
        elif job.status == RecipientListImportJobStatus.FAILED:
            failed += 1
    return {"processed": processed, "succeeded": succeeded, "failed": failed}


def process_recipient_list_import_job(job_id, *, timeout=DEFAULT_IMPORT_FETCH_TIMEOUT, max_bytes=DEFAULT_IMPORT_MAX_BYTES):
    with transaction.atomic():
        job = RecipientListImportJob.objects.select_for_update().get(pk=job_id)
        if job.status in {RecipientListImportJobStatus.SUCCEEDED, RecipientListImportJobStatus.FAILED}:
            return job
        job.status = RecipientListImportJobStatus.PROCESSING
        job.started_at = job.started_at or timezone.now()
        job.error = ""
        job.save(update_fields=["status", "started_at", "error", "updated_at"])

    try:
        content = fetch_import_content(job.source_url, timeout=timeout, max_bytes=max_bytes)
        parsed_members, failed_rows = parse_import_members(content)
        if failed_rows:
            return mark_import_failed(
                job.id,
                "row_validation_failed",
                row_count=len(parsed_members) + len(failed_rows),
                failed_rows=failed_rows,
                content_sha256=hashlib.sha256(content).hexdigest(),
            )
        return apply_import_members(job.id, parsed_members, content_sha256=hashlib.sha256(content).hexdigest())
    except Exception as exc:
        return mark_import_failed(job.id, str(exc))


def fetch_import_content(source_url, *, timeout, max_bytes):
    try:
        with urlopen(source_url, timeout=timeout) as response:
            content = response.read(max_bytes + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"fetch_failed: {exc}") from exc
    if len(content) > max_bytes:
        raise RuntimeError("fetch_failed: response_too_large")
    return content


def parse_import_members(content):
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("decode_failed: expected_utf8") from exc

    parsed_members = []
    failed_rows = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw_member = json.loads(line)
            if not isinstance(raw_member, dict):
                raise ApiValidationError({f"line.{line_number}": "must_be_object"})
            source_object_key = validate_path_key(raw_member.get("source_object_key"), f"line.{line_number}.source_object_key")
            parsed_members.append((source_object_key, validate_member_payload(raw_member)))
        except JSONDecodeError:
            failed_rows.append({"line": line_number, "error": "invalid_json"})
        except ApiValidationError as exc:
            failed_rows.append({"line": line_number, "error": exc.errors})
    return parsed_members, failed_rows


@transaction.atomic
def apply_import_members(job_id, parsed_members, *, content_sha256):
    job = RecipientListImportJob.objects.select_for_update().select_related("client", "audience").get(pk=job_id)
    recipient_list, _ = RecipientList.objects.update_or_create(
        client=job.client,
        audience=job.audience,
        key=job.list_key,
        defaults=job.list_defaults,
    )

    incoming_keys = set()
    created_count = 0
    updated_count = 0
    for source_object_key, member_data in parsed_members:
        incoming_keys.add(source_object_key)
        member, created = upsert_member(recipient_list, source_object_key, member_data)
        upsert_cascade_memberships(recipient_list, member)
        if created:
            created_count += 1
        else:
            updated_count += 1

    removed_count = 0
    if job.remove_absent:
        absent = list(
            recipient_list.members.select_related("contact")
            .filter(active=True)
            .exclude(source_object_key__in=incoming_keys)
        )
        removed_at = timezone.now()
        for member in absent:
            member.active = False
            member.removed_at = removed_at
            member.save(update_fields=["active", "removed_at", "updated_at"])
            upsert_cascade_memberships(recipient_list, member)
        removed_count = len(absent)
        recipient_list.last_reconciled_at = timezone.now()
        recipient_list.save(update_fields=["last_reconciled_at", "updated_at"])

    refresh_recipient_list_counts(recipient_list)
    job.recipient_list = recipient_list
    job.status = RecipientListImportJobStatus.SUCCEEDED
    job.row_count = len(parsed_members)
    job.created_count = created_count
    job.updated_count = updated_count
    job.removed_count = removed_count
    job.failed_count = 0
    job.failed_rows = []
    job.content_sha256 = content_sha256
    job.error = ""
    job.completed_at = timezone.now()
    job.save(
        update_fields=[
            "recipient_list",
            "status",
            "row_count",
            "created_count",
            "updated_count",
            "removed_count",
            "failed_count",
            "failed_rows",
            "content_sha256",
            "error",
            "completed_at",
            "updated_at",
        ]
    )
    return job


@transaction.atomic
def mark_import_failed(job_id, error, *, row_count=0, failed_rows=None, content_sha256=""):
    job = RecipientListImportJob.objects.select_for_update().get(pk=job_id)
    job.status = RecipientListImportJobStatus.FAILED
    job.row_count = row_count
    job.failed_rows = failed_rows or []
    job.failed_count = len(job.failed_rows)
    job.content_sha256 = content_sha256
    job.error = error[:5000]
    job.completed_at = timezone.now()
    job.save(
        update_fields=[
            "status",
            "row_count",
            "failed_rows",
            "failed_count",
            "content_sha256",
            "error",
            "completed_at",
            "updated_at",
        ]
    )
    return job
