import json

import pytest
from django.contrib.auth import get_user_model
from django.urls import resolve, reverse

from mailing.services.api_docs import API_DOC_PATHS, build_openapi_spec, route_path_map

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
    url = reverse("mailing:operator_api_docs")

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
    url = reverse("mailing:operator_api_docs_json")

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
    assert "/api/v1/contacts" in spec["paths"]


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
    ]
    for value in forbidden:
        assert value not in spec_text

    assert "BearerAuth" in spec_text
    assert '"scheme": "bearer"' in spec_text


def test_openapi_paths_do_not_include_unimplemented_public_campaign_api():
    spec = build_openapi_spec()
    assert all(not path.startswith("/api/v1/campaign") for path in spec["paths"])


def test_api_docs_page_does_not_render_secret_examples(client, staff_user):
    client.force_login(staff_user)
    response = client.get(reverse("mailing:operator_api_docs"))
    page = response.content.decode()

    assert response.status_code == 200
    assert "registration-welcome" in page
    assert "password-reset" in page
    assert "email-verification" in page
    assert "/api/v1/contacts/{contact_id}/verification" in page
    assert "https://client.example/verify/placeholder" in page
    assert "https://client.example/reset/placeholder" in page
    assert "Authorization: Token" not in page
    assert "api_key_hash" not in page
    assert "tracking_token_hash" not in page
    assert "unsubscribe_token_hash" not in page
    assert "verify/token" not in page
    assert "reset/token" not in page
