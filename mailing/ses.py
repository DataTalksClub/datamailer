import threading
import time
from email.message import EmailMessage
from email.message import Message

from django.conf import settings

_rate_limit_lock = threading.Lock()
_last_ses_send_monotonic = None


def send_email(
    *,
    ses_client,
    source,
    to_email,
    subject,
    html_body,
    text_body="",
    reply_to="",
    cc=None,
    bcc=None,
    headers=None,
    message_parts=None,
):
    throttle_ses_send()

    if headers or message_parts:
        return send_raw_email(
            ses_client=ses_client,
            source=source,
            to_email=to_email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            reply_to=reply_to,
            cc=cc,
            bcc=bcc,
            headers=headers or {},
            message_parts=message_parts or [],
        )

    body = {"Html": {"Charset": "UTF-8", "Data": html_body}}
    if text_body:
        body["Text"] = {"Charset": "UTF-8", "Data": text_body}

    params = {
        "Source": source,
        "Destination": {"ToAddresses": [to_email]},
        "Message": {
            "Subject": {"Charset": "UTF-8", "Data": subject},
            "Body": body,
        },
    }
    if settings.AWS_SES_CONFIGURATION_SET:
        params["ConfigurationSetName"] = settings.AWS_SES_CONFIGURATION_SET
    if reply_to:
        params["ReplyToAddresses"] = [reply_to]
    if cc:
        params["Destination"]["CcAddresses"] = cc
    if bcc:
        params["Destination"]["BccAddresses"] = bcc

    return ses_client.send_email(**params)["MessageId"]


def send_raw_email(
    *,
    ses_client,
    source,
    to_email,
    subject,
    html_body,
    text_body="",
    reply_to="",
    cc=None,
    bcc=None,
    headers=None,
    message_parts=None,
):
    cc = cc or []
    bcc = bcc or []
    headers = headers or {}
    message_parts = message_parts or []

    message = EmailMessage()
    message["From"] = source
    message["To"] = to_email
    if cc:
        message["Cc"] = ", ".join(cc)
    if reply_to:
        message["Reply-To"] = reply_to
    message["Subject"] = subject
    for name, value in headers.items():
        message[name] = value

    if text_body:
        message.set_content(text_body)
        if html_body:
            message.add_alternative(html_body, subtype="html")
    else:
        message.set_content(html_body, subtype="html")

    if message_parts:
        if message.get_content_maintype() != "multipart" or message.get_content_subtype() != "mixed":
            message.make_mixed()
        for part in message_parts:
            add_structured_part(message, part)

    params = {
        "Source": source,
        "Destinations": [to_email, *cc, *bcc],
        "RawMessage": {"Data": message.as_bytes()},
    }
    if settings.AWS_SES_CONFIGURATION_SET:
        params["ConfigurationSetName"] = settings.AWS_SES_CONFIGURATION_SET
    return ses_client.send_raw_email(**params)["MessageId"]


def add_structured_part(message, part):
    parsed = parse_content_type(part["content_type"])
    message.add_attachment(
        part["content"],
        subtype=parsed["subtype"],
        params=parsed["params"],
        filename=part.get("filename") or None,
        disposition=part.get("disposition") or "attachment",
    )


def parse_content_type(value):
    message = Message()
    message["content-type"] = value
    content_type = message.get_content_type()
    maintype, subtype = content_type.split("/", 1)
    return {
        "maintype": maintype,
        "subtype": subtype,
        "params": dict(message.get_params()[1:]),
    }


def throttle_ses_send():
    max_rate = float(getattr(settings, "SES_MAX_SEND_RATE_PER_SECOND", 0) or 0)
    if max_rate <= 0:
        return

    min_interval = 1.0 / max_rate
    global _last_ses_send_monotonic
    with _rate_limit_lock:
        now = time.monotonic()
        if _last_ses_send_monotonic is not None:
            elapsed = now - _last_ses_send_monotonic
            if elapsed < min_interval:
                wait_seconds = min_interval - elapsed
                time.sleep(wait_seconds)
                now += wait_seconds
        _last_ses_send_monotonic = now
