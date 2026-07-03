import json
from urllib.error import HTTPError, URLError

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from mailing.models import (
    Audience,
    Client,
    MailchimpSync,
    MailchimpSyncStatus,
    MailchimpTagMapping,
    Organization,
)
from mailing.services.auth import create_client_api_key
from mailing.services.mailchimp import (
    derive_datacenter,
    mailchimp_config,
    process_due_mailchimp_syncs,
    subscriber_hash,
)
from mailing.services.recipient_lists import (
    reconcile_recipient_list_for_client,
    upsert_recipient_list_member_for_client,
)

pytestmark = pytest.mark.django_db(transaction=True)

API_KEY = "test-client-key"


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


@pytest.fixture
def app_client(organization):
    client = Client.objects.create(
        organization=organization,
        name="DTC Courses",
        slug="dtc-courses",
        mailchimp_api_key="abc123def456-us21",
        mailchimp_list_id="listabc",
        mailchimp_enabled=True,
    )
    create_client_api_key(client=client, name="Test key", raw_api_key=API_KEY)
    return client


def member_data(email="learner@example.com", status="active"):
    return {
        "audience": "dtc-courses",
        "client": "dtc-courses",
        "list": {"type": "registrants", "name": "AI Dev Tools Zoomcamp 2026 registrants"},
        "member": {"email": email, "status": status},
    }


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --- Config helpers --------------------------------------------------------


def test_derive_datacenter():
    assert derive_datacenter("abc123-us21") == "us21"
    assert derive_datacenter("no-suffix-us5") == "us5"
    assert derive_datacenter("nodash") == ""
    assert derive_datacenter("") == ""


def test_mailchimp_config_requires_enabled_key_and_list(organization):
    client = Client.objects.create(organization=organization, name="C", slug="c")
    assert mailchimp_config(client) is None

    client.mailchimp_api_key = "key-us21"
    client.mailchimp_list_id = "list1"
    assert mailchimp_config(client) is None  # not enabled

    client.mailchimp_enabled = True
    config = mailchimp_config(client)
    assert config["datacenter"] == "us21"
    assert config["list_id"] == "list1"


def test_subscriber_hash_is_lowercase_md5():
    assert subscriber_hash("Learner@Example.com") == subscriber_hash("learner@example.com")


# --- Trigger / enqueue -----------------------------------------------------


def test_member_add_enqueues_sync_for_mapped_node(app_client, audience):
    MailchimpTagMapping.objects.create(
        client=app_client,
        audience=audience,
        list_key="ai-dev-tools-zoomcamp-2026:@registered",
        tag="ai-dev-tools-zoomcamp-2026",
    )

    upsert_recipient_list_member_for_client(
        "ai-dev-tools-zoomcamp-2026:@registered",
        "registration:1",
        member_data(),
        app_client,
    )

    syncs = list(MailchimpSync.objects.all())
    assert len(syncs) == 1
    sync = syncs[0]
    assert sync.tag == "ai-dev-tools-zoomcamp-2026"
    assert sync.list_key == "ai-dev-tools-zoomcamp-2026:@registered"
    assert sync.email == "learner@example.com"
    assert sync.mailchimp_list_id == "listabc"
    assert sync.status == MailchimpSyncStatus.PENDING


def test_no_mapping_enqueues_nothing(app_client, audience):
    upsert_recipient_list_member_for_client(
        "ai-dev-tools-zoomcamp-2026:@registered",
        "registration:1",
        member_data(),
        app_client,
    )
    assert MailchimpSync.objects.count() == 0


def test_disabled_client_enqueues_nothing(app_client, audience):
    app_client.mailchimp_enabled = False
    app_client.save(update_fields=["mailchimp_enabled"])
    MailchimpTagMapping.objects.create(
        client=app_client, audience=audience, list_key="ai-dev-tools-zoomcamp-2026:@registered", tag="t"
    )

    upsert_recipient_list_member_for_client(
        "ai-dev-tools-zoomcamp-2026:@registered", "registration:1", member_data(), app_client
    )
    assert MailchimpSync.objects.count() == 0


def test_deep_node_also_tags_ancestor_and_root(app_client, audience):
    MailchimpTagMapping.objects.create(
        client=app_client, audience=audience, list_key="course:@registered:hw1", tag="hw1-tag"
    )
    MailchimpTagMapping.objects.create(
        client=app_client, audience=audience, list_key="course:@registered", tag="course-tag"
    )
    MailchimpTagMapping.objects.create(client=app_client, audience=audience, list_key="<all>", tag="audience-tag")

    upsert_recipient_list_member_for_client(
        "course:@registered:hw1", "submission:1", member_data(), app_client
    )

    tags = set(MailchimpSync.objects.values_list("tag", flat=True))
    assert tags == {"hw1-tag", "course-tag", "audience-tag"}


def test_enqueue_is_idempotent_by_dedup_key(app_client, audience):
    MailchimpTagMapping.objects.create(
        client=app_client, audience=audience, list_key="course:@registered", tag="course-tag"
    )
    for _ in range(2):
        upsert_recipient_list_member_for_client(
            "course:@registered", "registration:1", member_data(), app_client
        )
    assert MailchimpSync.objects.count() == 1


def test_reconcile_path_enqueues_syncs(app_client, audience):
    MailchimpTagMapping.objects.create(
        client=app_client, audience=audience, list_key="course:@registered", tag="course-tag"
    )
    data = {
        "audience": "dtc-courses",
        "client": "dtc-courses",
        "list": {"type": "registrants", "name": "Registrants"},
        "members": [
            {"source_object_key": "registration:1", "email": "a@example.com"},
            {"source_object_key": "registration:2", "email": "b@example.com"},
        ],
    }
    reconcile_recipient_list_for_client("course:@registered", data, app_client)
    assert MailchimpSync.objects.filter(tag="course-tag").count() == 2


# --- Dispatcher ------------------------------------------------------------


def _enqueue_one(app_client, audience):
    MailchimpTagMapping.objects.create(
        client=app_client, audience=audience, list_key="course:@registered", tag="course-tag"
    )
    upsert_recipient_list_member_for_client("course:@registered", "registration:1", member_data(), app_client)
    return MailchimpSync.objects.get()


def test_dispatch_upserts_member_then_adds_tag(monkeypatch, app_client, audience):
    sync = _enqueue_one(app_client, audience)
    calls = []

    def fake_urlopen(request, *, timeout):
        calls.append((request.method, request.full_url, json.loads(request.data.decode()), dict(request.header_items())))
        return FakeResponse()

    monkeypatch.setattr("mailing.services.mailchimp.urlopen", fake_urlopen)
    result = process_due_mailchimp_syncs()

    assert result == {"processed": 1, "delivered": 1, "failed": 0}
    sync.refresh_from_db()
    assert sync.status == MailchimpSyncStatus.DELIVERED
    assert len(calls) == 2
    put_method, put_url, put_body, put_headers = calls[0]
    assert put_method == "PUT"
    assert put_url.endswith(f"/lists/listabc/members/{subscriber_hash('learner@example.com')}")
    assert put_body == {"email_address": "learner@example.com", "status_if_new": "subscribed"}
    assert put_headers["Authorization"].startswith("Basic ")
    post_method, post_url, post_body, _ = calls[1]
    assert post_method == "POST"
    assert post_url.endswith("/tags")
    assert post_body == {"tags": [{"name": "course-tag", "status": "active"}]}


def test_dispatch_permanent_failure_on_4xx(monkeypatch, app_client, audience):
    sync = _enqueue_one(app_client, audience)

    def fake_urlopen(request, *, timeout):
        raise HTTPError(request.full_url, 400, "Bad Request", {}, None)

    monkeypatch.setattr("mailing.services.mailchimp.urlopen", fake_urlopen)
    process_due_mailchimp_syncs()

    sync.refresh_from_db()
    assert sync.status == MailchimpSyncStatus.FAILED
    assert sync.attempt_count == 1
    assert sync.response_status == 400


def test_dispatch_retries_on_5xx(monkeypatch, app_client, audience):
    sync = _enqueue_one(app_client, audience)

    def fake_urlopen(request, *, timeout):
        raise HTTPError(request.full_url, 500, "Server Error", {}, None)

    monkeypatch.setattr("mailing.services.mailchimp.urlopen", fake_urlopen)
    process_due_mailchimp_syncs()

    sync.refresh_from_db()
    assert sync.status == MailchimpSyncStatus.PENDING
    assert sync.attempt_count == 1
    assert sync.response_status == 500


def test_dispatch_retries_on_transport_error(monkeypatch, app_client, audience):
    sync = _enqueue_one(app_client, audience)

    def fake_urlopen(request, *, timeout):
        raise URLError("connection refused")

    monkeypatch.setattr("mailing.services.mailchimp.urlopen", fake_urlopen)
    process_due_mailchimp_syncs()

    sync.refresh_from_db()
    assert sync.status == MailchimpSyncStatus.PENDING
    assert sync.attempt_count == 1


# --- API (set-only) --------------------------------------------------------


def auth(raw_key=API_KEY):
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def test_api_mailchimp_config_is_put_only(app_client, client):
    response = client.get(reverse("mailing:api_client_mailchimp"), **auth())
    assert response.status_code == 405


def test_api_set_mailchimp_config_does_not_return_key(app_client, client):
    response = client.put(
        reverse("mailing:api_client_mailchimp"),
        data=json.dumps({"api_key": "newkey-us9", "list_id": "listxyz", "enabled": True}),
        content_type="application/json",
        **auth(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["datacenter"] == "us9"
    assert body["list_id"] == "listxyz"
    assert body["api_key_set"] is True
    assert "api_key" not in body
    assert "newkey-us9" not in json.dumps(body)

    app_client.refresh_from_db()
    assert app_client.mailchimp_api_key == "newkey-us9"
    assert app_client.mailchimp_list_id == "listxyz"


def test_api_set_mailchimp_config_rejects_key_without_datacenter(app_client, client):
    response = client.put(
        reverse("mailing:api_client_mailchimp"),
        data=json.dumps({"api_key": "nodatacenter"}),
        content_type="application/json",
        **auth(),
    )
    assert response.status_code == 400
    assert response.json()["error"]["fields"]["api_key"] == "missing_datacenter_suffix"


def test_api_toggle_enabled_without_resending_key(app_client, client):
    response = client.put(
        reverse("mailing:api_client_mailchimp"),
        data=json.dumps({"enabled": False}),
        content_type="application/json",
        **auth(),
    )
    assert response.status_code == 200
    app_client.refresh_from_db()
    assert app_client.mailchimp_enabled is False
    assert app_client.mailchimp_api_key == "abc123def456-us21"  # preserved


def test_api_reconcile_tag_mappings(app_client, audience, client):
    response = client.put(
        reverse("mailing:api_client_mailchimp_tag_mappings"),
        data=json.dumps(
            {
                "audience": "dtc-courses",
                "mappings": [
                    {"list_key": "course:@registered", "tag": "course-tag"},
                    {"list_key": "<all>", "tag": "audience-tag", "enabled": False},
                ],
            }
        ),
        content_type="application/json",
        **auth(),
    )
    assert response.status_code == 200
    body = response.json()
    assert {m["list_key"] for m in body["mappings"]} == {"course:@registered", "<all>"}
    assert MailchimpTagMapping.objects.filter(client=app_client, audience=audience).count() == 2

    # Reconcile removes absent mappings.
    response = client.put(
        reverse("mailing:api_client_mailchimp_tag_mappings"),
        data=json.dumps(
            {"audience": "dtc-courses", "mappings": [{"list_key": "course:@registered", "tag": "course-tag"}]}
        ),
        content_type="application/json",
        **auth(),
    )
    assert response.status_code == 200
    assert MailchimpTagMapping.objects.filter(client=app_client, audience=audience).count() == 1


# --- Operator UI -----------------------------------------------------------


@pytest.fixture
def operator():
    return get_user_model().objects.create_user("operator", "op@example.com", "pw", is_staff=True)


def test_operator_edit_form_saves_config_and_keeps_key_when_blank(app_client, operator, client):
    client.force_login(operator)
    response = client.post(
        reverse("mailing:client_edit", args=[app_client.id]),
        data={
            "organization": app_client.organization_id,
            "name": "DTC Courses",
            "slug": "dtc-courses",
            "default_sender_id": "",
            "sender_emails": "",
            "cmp_webhook_url": "",
            "cmp_webhook_token": "",
            "mailchimp_api_key": "",  # blank -> keep existing
            "mailchimp_list_id": "newlist",
            "mailchimp_enabled": "on",
            "is_active": "on",
        },
    )
    assert response.status_code == 302
    app_client.refresh_from_db()
    assert app_client.mailchimp_api_key == "abc123def456-us21"  # preserved
    assert app_client.mailchimp_list_id == "newlist"
    assert app_client.mailchimp_enabled is True


def test_operator_detail_renders_mailchimp_panel(app_client, audience, operator, client):
    MailchimpTagMapping.objects.create(
        client=app_client, audience=audience, list_key="course:@registered", tag="course-tag"
    )
    client.force_login(operator)
    response = client.get(reverse("mailing:client_detail", args=[app_client.id]))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Mailchimp sync" in body
    assert "course:@registered = course-tag" in body


def test_operator_save_tag_mappings(app_client, audience, operator, client):
    client.force_login(operator)
    response = client.post(
        reverse("mailing:client_mailchimp_tag_mappings", args=[app_client.id]),
        data={
            "audience_id": audience.id,
            "mappings": "course:@registered = course-tag\n<all> = audience-tag\n# junk line\n",
        },
    )
    assert response.status_code == 302
    tags = set(
        MailchimpTagMapping.objects.filter(client=app_client, audience=audience).values_list("tag", flat=True)
    )
    assert tags == {"course-tag", "audience-tag"}
