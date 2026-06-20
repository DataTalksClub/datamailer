"""Real inbox: a *really delivered* test mailbox for end-to-end tests.

Where the mock inbox (``mailing.services.mock_inbox``) deliberately *skips* SES and
inspects the ``TransactionalMessage`` rows Datamailer already stores, the real inbox
proves the opposite end of the contract: that an email was **actually sent through
SES and received in a real mailbox**.

The receiving side is infrastructure (see ``datamailer-infra``): an SES receipt rule
for an inbound domain (default ``mailer.dtcdev.click``) writes every raw MIME message
to an S3 bucket under a prefix. This module is the read path over that bucket:

* :func:`is_real_inbox_address` - recognise a designated real-test address so the
  worker keeps it on the SES send path (and the mock inbox never short-circuits it);
* :func:`list_received_messages` / :func:`get_received_message` - poll the S3 bucket,
  parse the raw MIME, and return the messages received for one real-test address;
* :func:`clear_received_messages` - delete the S3 objects for an address (teardown).

Addresses are matched on the sub-addressed domain, so ``e2e+<tag>@<domain>`` and
``datamailer+<tag>@<domain>`` both count when ``<domain>`` is ``REAL_INBOX_DOMAIN``.
The unique ``<tag>`` is how an e2e run isolates its own mail.
"""

import email
from email.utils import getaddresses, parsedate_to_datetime

from django.conf import settings

from mailing.aws import s3_client
from mailing.services.api import ApiValidationError, isoformat
from mailing.services.contacts import normalize_email

DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 200


def real_inbox_enabled():
    return bool(getattr(settings, "REAL_INBOX_ENABLED", False))


def _real_domain():
    return (getattr(settings, "REAL_INBOX_DOMAIN", "") or "").strip().lower()


def _bucket():
    return (getattr(settings, "REAL_INBOX_S3_BUCKET", "") or "").strip()


def _prefix():
    return (getattr(settings, "REAL_INBOX_S3_PREFIX", "") or "").strip()


def _max_scan():
    return int(getattr(settings, "REAL_INBOX_MAX_SCAN_OBJECTS", 200) or 200)


def is_real_inbox_address(addr):
    """Return True when ``addr`` is a real-inbox (SES inbound) test address.

    True when its domain (ignoring any ``+tag`` sub-address) equals
    ``REAL_INBOX_DOMAIN``. Such an address is *really delivered* through SES so it can
    be received back from the inbound S3 bucket; it must never be short-circuited by
    the mock inbox. Independent of ``REAL_INBOX_ENABLED`` (which only gates the read
    API), so the send path stays real even where the read API is off.
    """
    domain = _real_domain()
    if not domain:
        return False
    if not isinstance(addr, str) or "@" not in addr:
        return False
    _, _, addr_domain = normalize_email(addr).partition("@")
    return addr_domain == domain


def address_tag(addr):
    """Return the ``+tag`` sub-address of a real-inbox address, or "" if none."""
    local = normalize_email(addr).partition("@")[0]
    return local.partition("+")[2] if "+" in local else ""


def _require_real_address(data):
    address = data.get("address") or data.get("email")
    if not isinstance(address, str) or not address.strip():
        raise ApiValidationError({"address": "required"})
    normalized = normalize_email(address)
    if not is_real_inbox_address(normalized):
        raise ApiValidationError({"address": "not_a_real_inbox_address"}, status_code=422)
    return normalized


def _validate_limit(raw_limit):
    if raw_limit in (None, ""):
        return DEFAULT_LIST_LIMIT
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ApiValidationError({"limit": "must_be_integer"}) from exc
    if limit < 1:
        raise ApiValidationError({"limit": "must_be_positive"})
    return min(limit, MAX_LIST_LIMIT)


def _require_bucket():
    bucket = _bucket()
    if not bucket:
        raise ApiValidationError(
            {"config": "real_inbox_s3_bucket_not_configured"},
            status_code=503,
        )
    return bucket


def _recipients(message):
    """All addresses in the To/Cc/Bcc/X-Original-To headers, normalized."""
    raw = []
    for header in ("To", "Cc", "Bcc", "X-Original-To", "Delivered-To"):
        raw.extend(message.get_all(header, []))
    return {normalize_email(addr) for _, addr in getaddresses(raw) if addr}


def _received_at(message):
    try:
        parsed = parsedate_to_datetime(message.get("Date", ""))
    except (TypeError, ValueError):
        return None
    return isoformat(parsed) if parsed else None


def _extract_bodies(message):
    text_body = ""
    html_body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain" and not text_body:
                text_body = _decode_part(part)
            elif content_type == "text/html" and not html_body:
                html_body = _decode_part(part)
    else:
        payload = _decode_part(message)
        if message.get_content_type() == "text/html":
            html_body = payload
        else:
            text_body = payload
    return text_body, html_body


def _decode_part(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _parse_message(key, raw_bytes):
    message = email.message_from_bytes(raw_bytes)
    text_body, html_body = _extract_bodies(message)
    from_addrs = getaddresses(message.get_all("From", []))
    return {
        "s3_key": key,
        "message_id": message.get("Message-ID", ""),
        "from_email": (from_addrs[0][1] if from_addrs else ""),
        "to": sorted(_recipients(message)),
        "subject": message.get("Subject", ""),
        "received_at": _received_at(message),
        "text_body": text_body,
        "html_body": html_body,
        "spam_verdict": message.get("X-SES-Spam-Verdict", ""),
        "virus_verdict": message.get("X-SES-Virus-Verdict", ""),
    }


def _summary(detail):
    return {k: detail[k] for k in ("s3_key", "message_id", "from_email", "to", "subject", "received_at")}


def _iter_recent_objects(client, bucket, prefix, max_scan):
    """Yield the (key, last_modified) of the most recently modified objects.

    S3 list order is lexicographic by key, not by time, so we collect up to a bounded
    number of keys and sort by LastModified descending. Inbound mail keys are random,
    so there is no time-ordered prefix to exploit.
    """
    paginator = client.get_paginator("list_objects_v2")
    objects = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("/"):
                continue
            objects.append((obj["Key"], obj["LastModified"]))
    objects.sort(key=lambda item: item[1], reverse=True)
    return objects[:max_scan]


def _fetch_messages_for_address(address, *, limit=None, client=None):
    bucket = _require_bucket()
    prefix = _prefix()
    client = client or s3_client()
    matches = []
    for key, _ in _iter_recent_objects(client, bucket, prefix, _max_scan()):
        raw = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        detail = _parse_message(key, raw)
        if address in detail["to"]:
            matches.append(detail)
            if limit is not None and len(matches) >= limit:
                break
    return matches


def list_received_messages(data, authenticated_client=None, *, client=None):
    """List messages really received at a real-inbox address (newest first).

    ``authenticated_client`` is accepted for view symmetry but unused: received mail is
    not scoped to a Datamailer client, only to the recipient address. Accepts
    ``address`` (or ``email``) and an optional ``limit``. Raises ``ApiValidationError``
    (422) when the address is not a real-inbox address, so a misconfigured test fails
    loudly instead of polling an address that is delivered to a real human inbox.
    """
    address = _require_real_address(data)
    limit = _validate_limit(data.get("limit"))
    messages = _fetch_messages_for_address(address, limit=limit, client=client)
    return {
        "address": address,
        "count": len(messages),
        "messages": [_summary(message) for message in messages],
    }


def get_received_message(data, s3_key, authenticated_client=None, *, client=None):
    """Return the full parsed message for one S3 object, scoped to a real address."""
    address = _require_real_address(data)
    bucket = _require_bucket()
    client = client or s3_client()
    try:
        raw = client.get_object(Bucket=bucket, Key=s3_key)["Body"].read()
    except client.exceptions.NoSuchKey as exc:
        raise ApiValidationError({"s3_key": "not_found"}, status_code=404) from exc
    detail = _parse_message(s3_key, raw)
    if address not in detail["to"]:
        raise ApiValidationError({"s3_key": "not_found"}, status_code=404)
    return {"message": detail}


def clear_received_messages(data, authenticated_client=None, *, client=None):
    """Delete every received S3 object for a real-inbox address (test teardown)."""
    address = _require_real_address(data)
    bucket = _require_bucket()
    client = client or s3_client()
    deleted = 0
    for detail in _fetch_messages_for_address(address, client=client):
        client.delete_object(Bucket=bucket, Key=detail["s3_key"])
        deleted += 1
    return {"address": address, "deleted_count": deleted}
