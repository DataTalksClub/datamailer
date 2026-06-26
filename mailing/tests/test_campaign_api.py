import pytest
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignStatus,
    Client,
    Contact,
    Organization,
    Subscription,
    SubscriptionStatus,
)
from mailing.services.auth import create_client_api_key

pytestmark = pytest.mark.django_db(transaction=True)

API_KEY = "test-client-key"


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def api_client_record(organization):
    client = Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
    )
    create_client_api_key(client=client, name="Campaign test", raw_api_key=API_KEY)
    return client


@pytest.fixture
def audience(organization):
    return Audience.objects.create(
        organization=organization,
        name="DataTalksClub",
        slug="datatalks-club",
    )


def auth_headers(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def put_campaign(django_client, external_key, payload, raw_key=API_KEY):
    return django_client.put(
        reverse("mailing:api_campaign", args=[external_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def get_campaign(django_client, external_key, payload, raw_key=API_KEY):
    return django_client.get(
        reverse("mailing:api_campaign", args=[external_key]),
        payload,
        **auth_headers(raw_key),
    )


def queue_campaign_api(django_client, external_key, payload, raw_key=API_KEY):
    return django_client.post(
        reverse("mailing:api_campaign_queue", args=[external_key]),
        data=payload,
        content_type="application/json",
        **auth_headers(raw_key),
    )


def campaign_payload(audience, api_client_record):
    return {
        "audience": audience.slug,
        "client": api_client_record.slug,
        "subject": "Course starts tomorrow",
        "preview_text": "Start details",
        "html_body": "<p>Hello learner</p>",
        "text_body": "Hello learner",
        "include_tags": ["python", "ml", "python"],
        "exclude_tags": ["inactive"],
    }


def test_campaign_api_upserts_and_gets_by_external_key(client, audience, api_client_record):
    response = put_campaign(
        client,
        "cmp-course-start-2026",
        campaign_payload(audience, api_client_record),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    campaign = body["campaign"]
    assert campaign["external_key"] == "cmp-course-start-2026"
    assert campaign["audience"] == audience.slug
    assert campaign["client"] == api_client_record.slug
    assert campaign["status"] == CampaignStatus.DRAFT
    assert campaign["include_tags"] == ["ml", "python"]
    assert campaign["exclude_tags"] == ["inactive"]

    update_payload = campaign_payload(audience, api_client_record) | {
        "subject": "Course starts today",
        "include_tags": ["ml"],
    }
    update = put_campaign(client, "cmp-course-start-2026", update_payload)

    assert update.status_code == 200
    assert update.json()["created"] is False
    assert update.json()["campaign"]["subject"] == "Course starts today"
    assert update.json()["campaign"]["include_tags"] == ["ml"]

    fetched = get_campaign(
        client,
        "cmp-course-start-2026",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert fetched.status_code == 200
    assert fetched.json()["campaign"]["subject"] == "Course starts today"
    assert Campaign.objects.count() == 1


def test_campaign_api_rejects_cross_audience_update(client, organization, audience, api_client_record):
    other_audience = Audience.objects.create(
        organization=organization,
        name="Other",
        slug="other",
    )
    Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="cmp-course-start-2026",
        subject="Existing",
        html_body="<p>Hello</p>",
    )
    payload = campaign_payload(other_audience, api_client_record)

    response = put_campaign(client, "cmp-course-start-2026", payload)

    assert response.status_code == 409
    assert response.json()["error"]["fields"] == {"external_key": "audience_mismatch"}


def test_campaign_api_rejects_update_after_queue(client, audience, api_client_record):
    Campaign.objects.create(
        client=api_client_record,
        audience=audience,
        external_key="queued-campaign",
        subject="Queued",
        html_body="<p>Hello</p>",
        status=CampaignStatus.QUEUED,
    )

    response = put_campaign(
        client,
        "queued-campaign",
        campaign_payload(audience, api_client_record),
    )

    assert response.status_code == 409
    assert response.json()["error"]["fields"] == {"status": "not_editable"}


def test_campaign_api_queue_snapshots_and_enqueues_pending_recipients(
    client,
    audience,
    api_client_record,
    monkeypatch,
):
    enqueued = []
    monkeypatch.setattr("mailing.services.campaigns.enqueue_campaign_email", enqueued.append)
    contact = Contact.objects.create(
        email="learner@example.com",
        verified_at=timezone.now(),
    )
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=api_client_record,
        status=SubscriptionStatus.SUBSCRIBED,
    )
    put = put_campaign(
        client,
        "cmp-course-start-2026",
        campaign_payload(audience, api_client_record) | {
            "include_tags": [],
            "exclude_tags": [],
        },
    )
    assert put.status_code == 201

    response = queue_campaign_api(
        client,
        "cmp-course-start-2026",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["queued"] is True
    assert body["batch_count"] == 1
    assert body["recipient_count"] == 1
    assert body["skipped_count"] == 0
    assert body["campaign"]["status"] == CampaignStatus.QUEUED
    assert len(enqueued) == 1
    assert enqueued[0]["contract"] == "campaign-email"
    assert enqueued[0]["campaign_recipient_ids"]

    replay = queue_campaign_api(
        client,
        "cmp-course-start-2026",
        {"audience": audience.slug, "client": api_client_record.slug},
    )

    assert replay.status_code == 409
    assert replay.json()["error"]["fields"] == {"status": "not_queueable"}
