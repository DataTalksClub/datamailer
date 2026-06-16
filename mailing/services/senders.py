from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from mailing.services.api import ApiValidationError


def normalize_sender_email(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ApiValidationError({"from_email": "must_be_string"})
    email = value.strip()
    try:
        validate_email(email)
    except ValidationError as exc:
        raise ApiValidationError({"from_email": "invalid"}) from exc
    return email


def configured_sender_emails(client):
    senders = []
    if client.default_from_email:
        senders.append(client.default_from_email)
    senders.extend(client.allowed_from_emails or [])
    return {normalize_sender_email(sender) for sender in senders if sender}


def resolve_sender_email(client, requested_from_email=""):
    requested_from_email = normalize_sender_email(requested_from_email)
    if not requested_from_email:
        return client.default_from_email or settings.DEFAULT_FROM_EMAIL

    if requested_from_email not in configured_sender_emails(client):
        raise ApiValidationError({"from_email": "not_allowed"})
    return requested_from_email
