from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from mailing.models import RecipientList, RecipientListMember, RecipientListType
from mailing.services.api import ApiValidationError, isoformat, validate_contact_scope
from mailing.services.contacts import normalize_email, upsert_contact


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


def refresh_recipient_list_counts(recipient_list):
    counts = {
        "member_count": recipient_list.members.count(),
        "active_member_count": recipient_list.members.filter(active=True).count(),
    }
    RecipientList.objects.filter(pk=recipient_list.pk).update(**counts, updated_at=timezone.now())
    for field, value in counts.items():
        setattr(recipient_list, field, value)
    return recipient_list


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

    removed_at = None
    if not member_data["active"]:
        removed_at = member.removed_at if member and member.removed_at else timezone.now()
    defaults = {
        "contact": contact,
        "email": normalize_email(member_data["email"]),
        "source_object_key": source_object_key,
        "metadata": member_data["metadata"],
        "active": member_data["active"],
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
    return {
        "recipient_list": recipient_list_payload(recipient_list),
        "member": recipient_list_member_payload(member),
        "created": created,
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
        upsert_member(recipient_list, source_object_key, member_data)

    removed_count = 0
    if remove_absent:
        absent = recipient_list.members.filter(active=True).exclude(source_object_key__in=incoming_keys)
        removed_count = absent.update(active=False, removed_at=timezone.now(), updated_at=timezone.now())

    recipient_list.last_reconciled_at = timezone.now()
    recipient_list.save(update_fields=["last_reconciled_at", "updated_at"])
    refresh_recipient_list_counts(recipient_list)
    return {
        "recipient_list": recipient_list_payload(recipient_list),
        "dry_run": False,
        "upsert_count": len(parsed_members),
        "removed_count": removed_count,
    }
