from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.core.validators import validate_email, validate_slug
from django.utils.text import slugify

from mailing.services.api import ApiValidationError


@dataclass(frozen=True)
class SenderSelection:
    sender_id: str
    email: str


def normalize_sender_id(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ApiValidationError({"from_email": "must_be_string"})
    sender_id = value.strip()
    try:
        validate_slug(sender_id)
    except ValidationError as exc:
        raise ApiValidationError({"from_email": "invalid"}) from exc
    return sender_id


def normalize_sender_email(value):
    email = str(value or "").strip()
    if not email:
        return ""
    try:
        validate_email(email)
    except ValidationError as exc:
        raise ApiValidationError({"sender_emails": "invalid_email"}) from exc
    return email


def sender_id_from_email(email):
    local_part = str(email).split("@", 1)[0]
    sender_id = slugify(local_part)
    return sender_id or "sender"


def configured_sender_map(client):
    senders = {}
    for item in client.sender_emails or []:
        if not isinstance(item, dict):
            continue
        sender_id = item.get("id")
        email = item.get("email")
        try:
            sender_id = normalize_sender_id(sender_id)
            email = normalize_sender_email(email)
        except ApiValidationError:
            continue
        if sender_id and email:
            senders[sender_id] = email
    return senders


def resolve_sender_email(client, requested_sender_id=""):
    sender_id = normalize_sender_id(requested_sender_id) or client.default_sender_id
    if not sender_id:
        raise ApiValidationError({"from_email": "not_configured"})

    email = configured_sender_map(client).get(sender_id)
    if not email:
        raise ApiValidationError({"from_email": "not_configured"})
    return SenderSelection(sender_id=sender_id, email=email)
