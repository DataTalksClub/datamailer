import threading
import time

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
):
    throttle_ses_send()

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

    return ses_client.send_email(**params)["MessageId"]


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
