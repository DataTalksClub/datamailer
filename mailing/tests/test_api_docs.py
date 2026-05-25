import json
import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import resolve, reverse

from mailing.services.api_docs import API_DOC_PATHS, build_openapi_spec, route_path_map, workflow_examples

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_user():
    return get_user_model().objects.create_user(
        username="staff",
        email="staff@example.com",
        password="password",
        is_staff=True,
    )


@pytest.fixture
def regular_user():
    return get_user_model().objects.create_user(
        username="regular",
        email="regular@example.com",
        password="password",
        is_staff=False,
    )


def test_api_docs_page_is_staff_only(client, staff_user, regular_user):
    url = reverse("mailing:api_docs")

    anonymous = client.get(url)
    assert anonymous.status_code == 302
    assert "/admin/login/" in anonymous["Location"]

    client.force_login(regular_user)
    non_staff = client.get(url)
    assert non_staff.status_code == 302
    assert "/admin/login/" in non_staff["Location"]

    client.force_login(staff_user)
    response = client.get(url)
    assert response.status_code == 200
    assert b"API Docs" in response.content
    assert b"OpenAPI JSON" in response.content


def test_openapi_json_is_staff_only_and_valid(client, staff_user, regular_user):
    url = reverse("mailing:api_docs_json")

    anonymous = client.get(url)
    assert anonymous.status_code == 302
    assert "/admin/login/" in anonymous["Location"]

    client.force_login(regular_user)
    non_staff = client.get(url)
    assert non_staff.status_code == 302
    assert "/admin/login/" in non_staff["Location"]

    client.force_login(staff_user)
    response = client.get(url)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/json")
    spec = response.json()
    assert spec["openapi"] == "3.1.0"
    assert spec["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
    assert "/api/contacts" in spec["paths"]
    assert all("/api/v1" not in path for path in spec["paths"])


def test_openapi_documented_paths_match_registered_routes():
    spec = build_openapi_spec()
    documented_paths = set(spec["paths"])

    assert documented_paths == set(API_DOC_PATHS.values())

    resolved_paths = route_path_map()
    assert set(resolved_paths) == documented_paths
    for documented_path, concrete_path in resolved_paths.items():
        assert resolve(concrete_path).url_name is not None, documented_path


def test_openapi_uses_bearer_auth_only_and_omits_forbidden_strings():
    spec_text = json.dumps(build_openapi_spec(), sort_keys=True)

    forbidden = [
        "Authorization: Token",
        "Token ",
        "AI Shipping Labs",
        "api_key_hash",
        "tracking_token_hash",
        "unsubscribe_token_hash",
        "legacy",
        "backwards compatibility",
        "operator",
    ]
    spec_text_lower = spec_text.lower()
    for value in forbidden:
        assert value.lower() not in spec_text_lower

    assert "BearerAuth" in spec_text
    assert '"scheme": "bearer"' in spec_text


def test_openapi_paths_do_not_include_unimplemented_public_campaign_api():
    spec = build_openapi_spec()
    assert all(not path.startswith("/api/campaign") for path in spec["paths"])
    assert all("/api/v1" not in path for path in spec["paths"])


def test_api_docs_page_does_not_render_secret_examples(client, staff_user):
    client.force_login(staff_user)
    response = client.get(reverse("mailing:api_docs"))
    page = response.content.decode()

    assert response.status_code == 200
    assert "registration-welcome" in page
    assert "password-reset" in page
    assert "email-verification" in page
    assert "/api/contacts/{contact_id}/verification" in page
    assert "/api/v1" not in page
    assert "https://client.example/verify/placeholder" in page
    assert "https://client.example/reset/placeholder" in page
    assert "Authorization: Token" not in page
    assert "operator" not in page.lower()
    assert "api_key_hash" not in page
    assert "tracking_token_hash" not in page
    assert "unsubscribe_token_hash" not in page
    assert "verify/token" not in page
    assert "reset/token" not in page


def test_api_docs_page_renders_runnable_workflow_examples(client, staff_user):
    client.force_login(staff_user)
    response = client.get(reverse("mailing:api_docs"))
    page = response.content.decode()

    assert response.status_code == 200
    assert "Setup and Authentication" in page
    assert "Client key management" in page
    assert "dm_dtccourses_demo_transactional_email_key" in page
    assert "dm_dtcnews_demo_newsletter_import_export_key" in page
    assert "Course platform transactional" in page
    assert "Newsletter import/export" in page

    expected_examples = [
        ("POST", "/api/contacts", "Create or update a contact"),
        ("GET", "/api/contacts/status", "Check contact status"),
        ("PATCH", "/api/contacts/{contact_id}/verification", "Mark email verified"),
        ("PATCH", "/api/contacts/{contact_id}/validation", "Set validation state"),
        ("PATCH", "/api/contacts/{contact_id}/suppression", "Set suppression state"),
        ("POST", "/api/subscriptions/subscribe", "Subscribe a contact"),
        ("POST", "/api/subscriptions/unsubscribe", "Unsubscribe a contact"),
        ("PUT", "/api/contacts/{contact_id}/tags", "Replace contact tags"),
        ("POST/DELETE", "/api/contacts/{contact_id}/tags/{tag_slug}", "Add and remove one tag"),
        ("POST", "/api/transactional/send", "Send transactional email"),
        ("POST", "/api/contacts/imports", "Import contacts with JSON"),
        ("POST", "/api/contacts/imports/csv", "Import contacts with CSV"),
        ("GET", "/api/contacts.csv", "Export contacts as CSV"),
        ("GET", "/api/contacts/{contact_id}/history", "Retrieve contact history"),
    ]
    for method, path, title in expected_examples:
        assert method in page
        assert path in page
        assert title in page

    assert "Curl" in page
    assert "Python" in page
    assert "Request" in page
    assert "Success response" in page
    assert "Common error" in page
    assert "validation_error" in page
    assert "invalid_api_key" in page
    assert "SQS_TRANSACTIONAL_EMAIL_QUEUE_URL" in page
    assert "LocalStack" in page
    assert "Endpoint Reference" in page
    assert "/api/v1" not in page


def test_api_docs_curl_examples_do_not_hard_code_contact_ids():
    hard_coded_contact_url = re.compile(r"/api/contacts/\d+(?:/|\\?|$)")

    for group in workflow_examples():
        for example in group["items"]:
            assert not hard_coded_contact_url.search(example["curl"]), example["id"]

    rendered_curls = "\n".join(example["curl"] for group in workflow_examples() for example in group["items"])
    assert "$CONTACT_ID" in rendered_curls
    assert "$NEWSLETTER_CONTACT_ID" in rendered_curls
    assert "python -c 'import json, sys; print(json.load(sys.stdin)[\"contact_id\"])'" in rendered_curls


def test_transactional_send_example_documents_queue_prerequisite(client, staff_user):
    client.force_login(staff_user)
    response = client.get(reverse("mailing:api_docs"))
    page = response.content.decode()

    assert response.status_code == 200
    assert "Send transactional email" in page
    assert "SQS_TRANSACTIONAL_EMAIL_QUEUE_URL" in page
    assert "default empty queue URL" in page
    assert "not runnable" in page


def test_api_docs_endpoint_reference_matches_openapi_paths(client, staff_user):
    client.force_login(staff_user)
    response = client.get(reverse("mailing:api_docs"))
    page = response.content.decode()
    spec = build_openapi_spec()

    for path in spec["paths"]:
        assert path in page

    assert "/api/campaigns" not in page
    assert "/api/v1" not in page
