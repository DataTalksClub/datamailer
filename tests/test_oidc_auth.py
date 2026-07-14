from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model

from datamailer import oidc

AUTH_SETTINGS = {
    "AUTH_BASE_URL": "https://auth.example.test",
    "AUTH_CLIENT_ID": "datamailer-client",
    "AUTH_CALLBACK_URL": "https://datamailer.example.test/auth/callback",
    "AUTH_LOGOUT_URL": "https://datamailer.example.test/",
    "AUTH_ISSUER": "https://issuer.example.test/pool",
    "AUTH_JWKS_URL": "https://issuer.example.test/pool/.well-known/jwks.json",
}


@pytest.mark.django_db
def test_oidc_login_uses_pkce_and_verified_callback_creates_staff_session(client, settings, monkeypatch):
    for key, value in AUTH_SETTINGS.items():
        setattr(settings, key, value)
    start = client.get("/auth/login?return_to=/admin/")
    assert start.status_code == 302
    authorize = urlparse(start["Location"])
    query = parse_qs(authorize.query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    nonce = client.session["oidc"]["nonce"]
    monkeypatch.setattr(oidc, "_exchange", lambda code, verifier: {"id_token": "signed-token"})
    monkeypatch.setattr(
        oidc,
        "_verify",
        lambda token: {
            "sub": "person-1",
            "email": "Person@DataTalks.Club",
            "email_verified": True,
            "nonce": nonce,
        },
    )
    callback = client.get(f"/auth/callback?code=valid&state={query['state'][0]}")
    assert callback.status_code == 302
    assert callback["Location"] == "/admin/"
    user = get_user_model().objects.get(email="person@datatalks.club")
    assert user.is_staff is True
    assert client.get("/admin/").status_code == 200


@pytest.mark.django_db
def test_oidc_callback_rejects_invalid_state(client, settings):
    for key, value in AUTH_SETTINGS.items():
        setattr(settings, key, value)
    response = client.get("/auth/callback?code=valid&state=wrong")
    assert response.status_code == 303
    assert response["Location"] == "/auth/error"
    assert response["Cache-Control"] == "no-store"
    assert response["Referrer-Policy"] == "no-referrer"
    assert response.content == b""


@pytest.mark.parametrize(
    "value",
    [
        "//evil.example",
        "/\\evil.example",
        "/%5cevil.example",
        "/%255cevil.example",
        "https://evil.example/",
        "/%2f%2fevil.example",
        "/auth/callback",
        "/auth/error",
        "/ok%0d%0aLocation:https://evil.example",
        "/bad%escape",
    ],
)
def test_oidc_return_to_rejects_unsafe_targets(value):
    assert oidc._safe_return_to(value) == "/admin/"


def test_oidc_return_to_accepts_clean_local_path_and_query():
    assert oidc._safe_return_to("/admin/mailing/campaign/?page=2#ignored") == "/admin/mailing/campaign/?page=2"


def test_oidc_error_page_is_clean_and_hardened(client):
    response = client.get("/auth/error?code=not-rendered&state=not-rendered")
    assert response.status_code == 403
    assert b"not-rendered" not in response.content
    assert b"Datamailer" in response.content
    assert response["Cache-Control"] == "no-store"
    assert response["Referrer-Policy"] == "no-referrer"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in response["Content-Security-Policy"]


def test_admin_login_redirects_to_shared_auth_when_configured(client, settings):
    for key, value in AUTH_SETTINGS.items():
        setattr(settings, key, value)
    response = client.get("/admin/login/")
    assert response.status_code == 302
    assert response["Location"] == "/auth/login?return_to=/admin/"
