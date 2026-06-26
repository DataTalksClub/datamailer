import json
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    Client,
    CmpCallback,
    CmpCallbackStatus,
    Contact,
    EmailEvent,
    EmailEventType,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services.cmp_callbacks import process_due_cmp_callbacks
from mailing.services.public_urls import (
    campaign_recipient_public_urls,
    click_redirect_url,
    open_pixel_url,
    unsubscribe_url,
)
from mailing.services.tokens import ensure_campaign_recipient_tokens, get_recipient_by_tracking_token, token_hash
from mailing.services.tracking import TRANSPARENT_GIF

pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def app_client(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def contact(audience, app_client):
    contact = Contact.objects.create(email="Person@example.com", verified_at=timezone.now())
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=app_client,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    return contact


@pytest.fixture
def campaign(audience, app_client):
    return Campaign.objects.create(audience=audience, client=app_client, subject="Weekly update")


@pytest.fixture
def recipient(campaign, contact):
    return CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.SENT,
        sent_at=timezone.now(),
    )


def test_token_generation_uses_random_hashes_only_and_preserves_existing_hashes(recipient):
    first = ensure_campaign_recipient_tokens(recipient)
    recipient.refresh_from_db()
    first_tracking_hash = recipient.tracking_token_hash
    first_unsubscribe_hash = recipient.unsubscribe_token_hash

    second = ensure_campaign_recipient_tokens(recipient)
    recipient.refresh_from_db()

    assert len(first.tracking_token) >= 40
    assert len(first.unsubscribe_token) >= 40
    assert first.tracking_token != first.unsubscribe_token
    assert recipient.tracking_token_hash == token_hash(first.tracking_token)
    assert recipient.unsubscribe_token_hash == token_hash(first.unsubscribe_token)
    assert first_tracking_hash == recipient.tracking_token_hash
    assert first_unsubscribe_hash == recipient.unsubscribe_token_hash
    assert second.tracking_token is None
    assert second.unsubscribe_token is None
    assert first.tracking_token not in {recipient.tracking_token_hash, recipient.unsubscribe_token_hash}
    assert first.unsubscribe_token not in {recipient.tracking_token_hash, recipient.unsubscribe_token_hash}


def test_random_token_generation_is_not_deterministic(campaign, contact):
    first_recipient = CampaignRecipient.objects.create(campaign=campaign, contact=contact, email=contact.email)
    second_contact = Contact.objects.create(email="other@example.com", verified_at=timezone.now())
    second_recipient = CampaignRecipient.objects.create(
        campaign=campaign, contact=second_contact, email=second_contact.email
    )

    first_tokens = ensure_campaign_recipient_tokens(first_recipient)
    second_tokens = ensure_campaign_recipient_tokens(second_recipient)

    assert first_tokens.tracking_token != second_tokens.tracking_token
    assert first_tokens.unsubscribe_token != second_tokens.unsubscribe_token


def test_hash_lookup_finds_recipient_without_persisting_raw_token(recipient):
    tokens = ensure_campaign_recipient_tokens(recipient)
    recipient.refresh_from_db()

    assert get_recipient_by_tracking_token(tokens.tracking_token) == recipient
    assert get_recipient_by_tracking_token(recipient.tracking_token_hash) is None
    assert tokens.tracking_token not in CampaignRecipient.objects.values_list("tracking_token_hash", flat=True)


@override_settings(PUBLIC_BASE_URL="https://mail.example.com/")
def test_public_url_helpers_generate_absolute_email_urls(recipient):
    destination = "https://example.com/course?a=1&b=two"
    urls = campaign_recipient_public_urls(recipient, destination)
    tracking_token = urlparse(urls["open_pixel_url"]).path.removeprefix("/t/o/").removesuffix(".gif")
    unsubscribe_token = urlparse(urls["unsubscribe_url"]).path.removeprefix("/unsubscribe/")

    assert open_pixel_url(tracking_token) == f"https://mail.example.com/t/o/{tracking_token}.gif"
    assert unsubscribe_url(unsubscribe_token) == f"https://mail.example.com/unsubscribe/{unsubscribe_token}"

    click_url = click_redirect_url(tracking_token, destination)
    parsed = urlparse(click_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "mail.example.com"
    assert parsed.path == f"/t/c/{tracking_token}"
    assert parse_qs(parsed.query) == {"u": [destination]}

    assert urls["open_pixel_url"].endswith(f"/t/o/{tracking_token}.gif")
    assert urls["click_redirect_url"] == click_url
    assert urls["unsubscribe_url"].endswith(f"/unsubscribe/{unsubscribe_token}")


def test_public_url_helper_does_not_rotate_existing_hashes(recipient):
    tokens = ensure_campaign_recipient_tokens(recipient)
    recipient.refresh_from_db()
    tracking_hash = recipient.tracking_token_hash
    unsubscribe_hash = recipient.unsubscribe_token_hash

    with pytest.raises(ValueError):
        campaign_recipient_public_urls(recipient)

    recipient.refresh_from_db()
    assert recipient.tracking_token_hash == tracking_hash
    assert recipient.unsubscribe_token_hash == unsubscribe_hash
    assert tokens.tracking_token is not None


def test_open_pixel_records_repeated_open_events_and_unique_state_once(client, recipient):
    token = ensure_campaign_recipient_tokens(recipient).tracking_token
    url = reverse("mailing:tracking_open", args=[token])

    first = client.get(url)
    second = client.get(url)

    recipient.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert first.status_code == 200
    assert first.headers["Content-Type"] == "image/gif"
    assert first.content == TRANSPARENT_GIF
    assert "no-store" in first.headers["Cache-Control"]
    assert second.status_code == 200
    assert recipient.open_count == 2
    assert recipient.first_opened_at is not None
    assert recipient.campaign.open_count == 2
    assert recipient.campaign.unique_open_count == 1
    assert EmailEvent.objects.filter(event_type=EmailEventType.OPEN, campaign_recipient=recipient).count() == 2


def test_open_pixel_invalid_token_is_safe_noop(client, recipient):
    response = client.get(reverse("mailing:tracking_open", args=["missing-token"]))
    malformed_response = client.get(reverse("mailing:tracking_open", args=["bad-token-\u2603"]))

    recipient.refresh_from_db()
    assert response.status_code == 404
    assert malformed_response.status_code == 404
    assert response.headers["Content-Type"] == "image/gif"
    assert recipient.open_count == 0
    assert EmailEvent.objects.count() == 0


def test_click_redirect_records_repeated_clicks_and_redirects(client, recipient):
    token = ensure_campaign_recipient_tokens(recipient).tracking_token
    url = reverse("mailing:tracking_click", args=[token])
    destination = "https://example.com/path?x=1"

    first = client.get(url, {"u": destination})
    second = client.get(url, {"u": destination})

    recipient.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert first.status_code == 302
    assert first.headers["Location"] == destination
    assert second.status_code == 302
    assert recipient.click_count == 2
    assert recipient.first_clicked_at is not None
    assert recipient.campaign.click_count == 2
    assert recipient.campaign.unique_click_count == 1
    assert list(EmailEvent.objects.values_list("event_type", "url")) == [
        (EmailEventType.CLICK, destination),
        (EmailEventType.CLICK, destination),
    ]


@override_settings(CMP_WEBHOOK_URL="https://cmp.example.com/api/datamailer/events", CMP_WEBHOOK_TOKEN="secret")
def test_tracking_open_and_click_emit_cmp_callbacks(client, recipient, monkeypatch):
    monkeypatch.setattr(
        "mailing.services.cmp_callbacks.transaction.on_commit",
        lambda callback: callback(),
    )
    token = ensure_campaign_recipient_tokens(recipient).tracking_token

    open_response = client.get(reverse("mailing:tracking_open", args=[token]))
    click_response = client.get(
        reverse("mailing:tracking_click", args=[token]),
        {"u": "https://example.com/path"},
    )

    assert open_response.status_code == 200
    assert click_response.status_code == 302
    assert list(CmpCallback.objects.order_by("id").values_list("event_type", flat=True)) == [
        "message.opened",
        "message.clicked",
    ]


@pytest.mark.parametrize(
    "destination", ["", "mailto:person@example.com", "javascript:alert(1)", "/relative", "https://"]
)
def test_click_redirect_rejects_missing_or_unsafe_urls(client, recipient, destination):
    token = ensure_campaign_recipient_tokens(recipient).tracking_token
    response = client.get(reverse("mailing:tracking_click", args=[token]), {"u": destination})

    recipient.refresh_from_db()
    assert response.status_code == 400
    assert recipient.click_count == 0
    assert EmailEvent.objects.count() == 0


def test_click_redirect_invalid_token_does_not_mutate(client, recipient):
    response = client.get(reverse("mailing:tracking_click", args=["missing-token"]), {"u": "https://example.com"})

    assert response.status_code == 400
    assert EmailEvent.objects.count() == 0


def test_unsubscribe_get_renders_public_page_without_login(client, recipient):
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token

    response = client.get(reverse("mailing:public_unsubscribe", args=[token]))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Choose which marketing emails to stop" in html
    assert "Person@example.com" in html
    assert "DTC Courses" in html
    assert "DataTalksClub" in html
    assert "Stop marketing emails from DTC Courses" in html
    assert "Stop marketing emails for DataTalksClub" in html
    assert "Stop all Datamailer-managed marketing emails" in html
    assert "Update marketing preferences" in html
    assert 'name="scope" value="client"' in html
    assert 'name="scope" value="audience"' in html
    assert 'name="scope" value="global"' in html
    assert "Campaigns" not in html
    assert "/admin/" not in html


@pytest.mark.parametrize("scope", ["client", "audience", "global"])
def test_unsubscribe_post_applies_scope_idempotently_and_records_events(client, recipient, contact, scope):
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token
    url = reverse("mailing:public_unsubscribe", args=[token])

    first = client.post(url, {"scope": scope})
    second = client.post(url, {"scope": scope})

    recipient.refresh_from_db()
    contact.refresh_from_db()
    recipient.campaign.refresh_from_db()
    assert first.status_code == 200
    assert second.status_code == 200
    html = first.content.decode()
    assert "Your marketing preference was updated" in html
    assert "Person@example.com has been unsubscribed from the selected marketing emails." in html
    assert "transactional or required account emails" in html
    assert "Campaigns" not in html
    assert "/admin/" not in html
    assert recipient.status == CampaignRecipientStatus.UNSUBSCRIBED
    assert recipient.campaign.unsubscribe_count == 1
    assert EmailEvent.objects.filter(event_type=EmailEventType.UNSUBSCRIBE, campaign_recipient=recipient).count() == 2

    if scope == "global":
        assert contact.global_unsubscribed_at is not None
    elif scope == "audience":
        subscription = Subscription.objects.get(
            contact=contact, audience=recipient.campaign.audience, client__isnull=True
        )
        assert subscription.status == SubscriptionStatus.UNSUBSCRIBED
        assert subscription.unsubscribe_reason == "public_unsubscribe"
    else:
        subscription = Subscription.objects.get(
            contact=contact, audience=recipient.campaign.audience, client=recipient.campaign.client
        )
        assert subscription.status == SubscriptionStatus.UNSUBSCRIBED
        assert subscription.unsubscribe_reason == "public_unsubscribe"


@override_settings(CMP_WEBHOOK_URL="https://cmp.example.com/api/datamailer/events", CMP_WEBHOOK_TOKEN="secret")
def test_unsubscribe_post_emits_cmp_callback(client, recipient, monkeypatch):
    posts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_urlopen(request, *, timeout):
        posts.append(
            {
                "url": request.full_url,
                "json": json.loads(request.data.decode("utf-8")),
                "headers": dict(request.header_items()),
                "timeout": timeout,
            }
        )
        return Response()

    monkeypatch.setattr(
        "mailing.services.cmp_callbacks.urlopen",
        fake_urlopen,
    )
    monkeypatch.setattr(
        "mailing.services.cmp_callbacks.transaction.on_commit",
        lambda callback: callback(),
    )
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token

    response = client.post(
        reverse("mailing:public_unsubscribe", args=[token]),
        {"scope": "client"},
    )

    assert response.status_code == 200
    assert CmpCallback.objects.filter(status=CmpCallbackStatus.PENDING).count() == 1
    process_due_cmp_callbacks()
    assert len(posts) == 1
    body = posts[0]["json"]
    assert posts[0]["url"] == "https://cmp.example.com/api/datamailer/events"
    assert posts[0]["headers"]["Authorization"] == "Bearer secret"
    assert body["event_type"] == "subscription.unsubscribed"
    assert body["email"] == recipient.contact.normalized_email
    assert body["audience"] == recipient.campaign.audience.slug
    assert body["client"] == recipient.campaign.client.slug
    assert body["metadata"]["scope"] == "client"


def test_unsubscribe_invalid_token_and_invalid_scope_do_not_mutate(client, recipient):
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token

    invalid_token_response = client.post(
        reverse("mailing:public_unsubscribe", args=["missing-token"]), {"scope": "global"}
    )
    invalid_scope_response = client.post(reverse("mailing:public_unsubscribe", args=[token]), {"scope": "bad"})
    invalid_token_html = invalid_token_response.content.decode()
    invalid_scope_html = invalid_scope_response.content.decode()

    recipient.refresh_from_db()
    assert invalid_token_response.status_code == 404
    assert "This unsubscribe link is no longer available" in invalid_token_html
    assert "Person@example.com" not in invalid_token_html
    assert "Campaigns" not in invalid_token_html
    assert "/admin/" not in invalid_token_html
    assert invalid_scope_response.status_code == 400
    assert "Select one of the unsubscribe options below" in invalid_scope_html
    assert "Stop marketing emails from DTC Courses" in invalid_scope_html
    assert "Stop marketing emails for DataTalksClub" in invalid_scope_html
    assert "Stop all Datamailer-managed marketing emails" in invalid_scope_html
    assert recipient.status == CampaignRecipientStatus.SENT
    assert EmailEvent.objects.count() == 0


def test_unsubscribe_invalid_scope_keeps_subscriptions_unchanged(client, recipient, contact):
    token = ensure_campaign_recipient_tokens(recipient).unsubscribe_token

    response = client.post(reverse("mailing:public_unsubscribe", args=[token]), {"scope": "bad"})

    recipient.refresh_from_db()
    contact.refresh_from_db()
    subscription = Subscription.objects.get(
        contact=contact, audience=recipient.campaign.audience, client=recipient.campaign.client
    )
    assert response.status_code == 400
    assert recipient.status == CampaignRecipientStatus.SENT
    assert contact.global_unsubscribed_at is None
    assert subscription.status == SubscriptionStatus.SUBSCRIBED
    assert subscription.unsubscribe_reason == ""
    assert EmailEvent.objects.count() == 0
