from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from mailing.models import RecipientList, RecipientListMember, RecipientListType
from mailing.services.api import ApiValidationError, isoformat, validate_contact_scope
from mailing.services.contacts import normalize_email, upsert_contact

ROOT_LIST_KEY = "<all>"
CASCADE_SOURCE_PREFIX = "cascade-contact:"


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
    upsert_cascade_memberships(recipient_list, member)
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
        upsert_member(recipient_list, source_object_key, member_data)

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
