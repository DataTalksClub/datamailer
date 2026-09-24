import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import unquote_plus

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from mailing.aws import aws_client, s3_client
from mailing.inbound_mime import parse_mime

CONTRACT = "inbound-email"
VERSION = 1
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def process_inbound_s3_notification(payload, *, s3=None, dynamodb=None, publisher=None):
    s3 = s3 or s3_client()
    dynamodb = dynamodb or aws_client("dynamodb")
    publisher = publisher or aws_client("sns")
    results = []
    for record in payload.get("Records", []):
        if record.get("eventSource") != "aws:s3":
            raise ValueError("inbound worker accepts only S3 event records")
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        results.extend(process_inbound_object(bucket, key, s3=s3, dynamodb=dynamodb, publisher=publisher))
    return results


def process_inbound_object(bucket, key, *, s3, dynamodb, publisher):
    raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    message = parse_mime(raw)
    if not message.message_id:
        raise ValueError("inbound MIME message is missing Message-ID")

    routes = matching_routes(message.recipient_addresses, settings.INBOUND_EMAIL_ROUTES)
    results = []
    for route, matched_recipients in routes.items():
        event_id = hashlib.sha256(f"{message.message_id}\0{route}".encode()).hexdigest()
        if not claim_event(dynamodb, event_id, route, message.message_id):
            results.append({"event_id": event_id, "route": route, "idempotent_replay": True})
            continue
        try:
            event = build_event(
                message,
                event_id=event_id,
                route=route,
                matched_recipients=matched_recipients,
                raw_bucket=bucket,
                raw_key=key,
                s3=s3,
            )
            publish_event(publisher, event)
        except Exception:
            release_claim(dynamodb, event_id)
            raise
        results.append({"event_id": event_id, "route": route, "idempotent_replay": False})
    return results


SAFE_ROUTE_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")


def matching_routes(recipients, configured_routes):
    matched = {}
    for recipient in recipients:
        route = configured_routes.get(recipient.lower()) or fallback_route(recipient)
        if route:
            matched.setdefault(route, []).append(recipient)
    return matched


def fallback_route(recipient):
    """Map the local part to a route for enabled trigger domains.

    Exact INBOUND_EMAIL_ROUTES entries always win. The fallback lets a
    whole domain accept ad-hoc addresses (Zapier-style email triggers):
    anything@dtcdev.click routes as "anything" without per-address config.
    """
    local, _, domain = recipient.lower().rpartition("@")
    if domain not in settings.INBOUND_EMAIL_ROUTE_DOMAINS:
        return None
    if not SAFE_ROUTE_RE.fullmatch(local):
        return None
    return local


def build_event(message, *, event_id, route, matched_recipients, raw_bucket, raw_key, s3):
    artifact_prefix = f"{settings.INBOUND_EMAIL_ARTIFACT_PREFIX.rstrip('/')}/{event_id}"
    body = {
        "text": content_value(message.body_text, "text/plain", artifact_prefix, "body.txt", raw_bucket, s3),
        "html": content_value(message.body_html, "text/html", artifact_prefix, "body.html", raw_bucket, s3),
    }
    attachments = []
    for index, attachment in enumerate(message.attachments):
        filename = safe_filename(attachment.filename or f"attachment-{index + 1}")
        object_key = f"{artifact_prefix}/attachments/{index + 1:03d}-{filename}"
        s3.put_object(
            Bucket=raw_bucket,
            Key=object_key,
            Body=attachment.payload,
            ContentType=attachment.content_type,
            ServerSideEncryption="AES256",
        )
        attachments.append(
            {
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "content_id": attachment.content_id,
                "disposition": attachment.disposition,
                "size": len(attachment.payload),
                "checksum": f"sha256:{hashlib.sha256(attachment.payload).hexdigest()}",
                "s3": {"bucket": raw_bucket, "key": object_key},
            }
        )
    return {
        "contract": CONTRACT,
        "version": VERSION,
        "event_id": event_id,
        "event_type": "email.received",
        "occurred_at": datetime.now(UTC).isoformat(),
        "route": route,
        "message_id": message.message_id,
        "sender": {"header": message.from_, "addresses": message.sender_addresses},
        "recipients": {
            "to": message.to,
            "cc": message.cc,
            "addresses": message.recipient_addresses,
            "matched": matched_recipients,
        },
        "subject": message.subject,
        "date": message.date,
        "body": body,
        "attachments": attachments,
        "raw_mime": {"bucket": raw_bucket, "key": raw_key},
    }


def content_value(value, content_type, prefix, filename, bucket, s3):
    encoded = value.encode()
    if len(encoded) <= settings.INBOUND_EMAIL_INLINE_BODY_MAX_BYTES:
        return {"content_type": content_type, "value": value, "size": len(encoded)}
    key = f"{prefix}/{filename}"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=encoded,
        ContentType=content_type,
        ServerSideEncryption="AES256",
    )
    return {"content_type": content_type, "size": len(encoded), "s3": {"bucket": bucket, "key": key}}


def claim_event(dynamodb, event_id, route, message_id):
    if not settings.INBOUND_EMAIL_IDEMPOTENCY_TABLE:
        raise ImproperlyConfigured("INBOUND_EMAIL_IDEMPOTENCY_TABLE is required")
    try:
        dynamodb.put_item(
            TableName=settings.INBOUND_EMAIL_IDEMPOTENCY_TABLE,
            Item={
                "event_id": {"S": event_id},
                "route": {"S": route},
                "message_id": {"S": message_id},
                "created_at": {"S": datetime.now(UTC).isoformat()},
            },
            ConditionExpression="attribute_not_exists(event_id)",
        )
    except dynamodb.exceptions.ConditionalCheckFailedException:
        return False
    return True


def release_claim(dynamodb, event_id):
    dynamodb.delete_item(TableName=settings.INBOUND_EMAIL_IDEMPOTENCY_TABLE, Key={"event_id": {"S": event_id}})


def publish_event(publisher, event):
    if not settings.INBOUND_EMAIL_EVENTS_TOPIC_ARN:
        raise ImproperlyConfigured("INBOUND_EMAIL_EVENTS_TOPIC_ARN is required")
    publisher.publish(
        TopicArn=settings.INBOUND_EMAIL_EVENTS_TOPIC_ARN,
        Message=json.dumps(event, sort_keys=True),
        MessageAttributes={
            "contract": {"DataType": "String", "StringValue": CONTRACT},
            "route": {"DataType": "String", "StringValue": event["route"]},
        },
    )


def safe_filename(value):
    return SAFE_FILENAME_RE.sub("_", value).strip("._") or "attachment"
