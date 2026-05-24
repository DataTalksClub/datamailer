from urllib.parse import urlencode

from django.conf import settings

from mailing.services.tokens import ensure_campaign_recipient_tokens


def _base_url():
    return settings.PUBLIC_BASE_URL.rstrip("/")


def open_pixel_url(tracking_token):
    return f"{_base_url()}/t/o/{tracking_token}.gif"


def click_redirect_url(tracking_token, destination_url):
    return f"{_base_url()}/t/c/{tracking_token}?{urlencode({'u': destination_url})}"


def unsubscribe_url(unsubscribe_token):
    return f"{_base_url()}/unsubscribe/{unsubscribe_token}"


def campaign_recipient_public_urls(recipient, destination_url=None):
    tokens = ensure_campaign_recipient_tokens(recipient)
    if not tokens.tracking_token or not tokens.unsubscribe_token:
        raise ValueError("Raw public tokens are unavailable for recipient with existing token hashes.")
    urls = {
        "open_pixel_url": open_pixel_url(tokens.tracking_token),
        "unsubscribe_url": unsubscribe_url(tokens.unsubscribe_token),
    }
    if destination_url is not None:
        urls["click_redirect_url"] = click_redirect_url(tokens.tracking_token, destination_url)
    return urls
