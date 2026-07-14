"""Shared Cognito login adapter for Datamailer's Django staff session."""

import base64
import hashlib
import hmac
import json
import secrets
import urllib.parse
import urllib.request

import jwt
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model, login, logout
from django.http import HttpResponse, HttpResponseRedirect
from django.views.decorators.http import require_GET


def configured():
    return all(
        getattr(settings, key, "")
        for key in ("AUTH_BASE_URL", "AUTH_CLIENT_ID", "AUTH_CALLBACK_URL", "AUTH_ISSUER", "AUTH_JWKS_URL")
    )


def _b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _safe_return_to(value):
    if not isinstance(value, str) or not value:
        return "/admin/"
    candidate = value
    for _ in range(8):
        decoded = urllib.parse.unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    controls = any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in candidate)
    malformed_escape = any(
        candidate[index] == "%"
        and (
            index + 2 >= len(candidate)
            or any(char not in "0123456789abcdefABCDEF" for char in candidate[index + 1 : index + 3])
        )
        for index in range(len(candidate))
    )
    if controls or malformed_escape or "\\" in candidate or candidate.startswith("//"):
        return "/admin/"
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return "/admin/"
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return "/admin/"
    if parsed.path == "/login" or parsed.path == "/logout" or parsed.path.startswith("/auth/"):
        return "/admin/"
    return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))


def _security_headers(response):
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _error_redirect():
    response = HttpResponseRedirect("/auth/error")
    response.status_code = 303
    return _security_headers(response)


@require_GET
def begin(request):
    if not configured():
        return HttpResponse("Shared authentication is not configured", status=503)
    state = _b64url(secrets.token_bytes(32))
    verifier = _b64url(secrets.token_bytes(48))
    nonce = _b64url(secrets.token_bytes(32))
    request.session["oidc"] = {
        "state": state,
        "verifier": verifier,
        "nonce": nonce,
        "return_to": _safe_return_to(request.GET.get("return_to")),
    }
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": settings.AUTH_CLIENT_ID,
            "redirect_uri": settings.AUTH_CALLBACK_URL,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
        }
    )
    return _security_headers(HttpResponseRedirect(f"{settings.AUTH_BASE_URL}/oauth2/authorize?{query}"))


def _exchange(code, verifier):
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": settings.AUTH_CLIENT_ID,
            "code": code,
            "redirect_uri": settings.AUTH_CALLBACK_URL,
            "code_verifier": verifier,
        }
    ).encode()
    request = urllib.request.Request(
        f"{settings.AUTH_BASE_URL}/oauth2/token",
        data=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def _verify(id_token):
    key = jwt.PyJWKClient(settings.AUTH_JWKS_URL).get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token,
        key.key,
        algorithms=["RS256"],
        audience=settings.AUTH_CLIENT_ID,
        issuer=settings.AUTH_ISSUER,
        options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )


@require_GET
def callback(request):
    pending = request.session.pop("oidc", None)
    code, state = request.GET.get("code", ""), request.GET.get("state", "")
    if not pending or not code or not hmac.compare_digest(state, str(pending.get("state", ""))):
        return _error_redirect()
    try:
        claims = _verify(_exchange(code, pending["verifier"])["id_token"])
    except Exception:
        return _error_redirect()
    if not hmac.compare_digest(str(claims.get("nonce", "")), str(pending.get("nonce", ""))):
        return _error_redirect()
    email = claims.get("email")
    if not isinstance(email, str) or claims.get("email_verified") is not True:
        return _error_redirect()
    email = email.lower()
    model = get_user_model()
    user = model.objects.filter(email__iexact=email).first()
    if user is None:
        user = model.objects.create_user(username=email, email=email)
    if not user.is_active:
        return _error_redirect()
    changed = []
    if user.email != email:
        user.email = email
        changed.append("email")
    if not user.is_staff:
        user.is_staff = True
        changed.append("is_staff")
    if changed:
        user.save(update_fields=changed)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return _security_headers(HttpResponseRedirect(_safe_return_to(pending.get("return_to"))))


@require_GET
def auth_error(_request):
    response = HttpResponse(
        """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign-in error · Datamailer</title><style>body{margin:0;font:16px system-ui;background:#f5f7fa;color:#172033}main{max-width:32rem;margin:10vh auto;padding:2rem;background:white;border:1px solid #dce2ea;border-radius:.75rem;box-shadow:0 8px 28px #17203314}a{color:#1769aa;font-weight:600}</style></head><body><main><h1>Sign-in error</h1><p>Authentication could not be completed. No account changes were made.</p><p><a href="/auth/login?return_to=/admin/">Try again</a></p></main></body></html>""",
        status=403,
        content_type="text/html; charset=utf-8",
    )
    response["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
    )
    return _security_headers(response)


@require_GET
def end(request):
    logout(request)
    if not configured() or not settings.AUTH_LOGOUT_URL:
        return HttpResponseRedirect("/admin/login/")
    query = urllib.parse.urlencode({"client_id": settings.AUTH_CLIENT_ID, "logout_uri": settings.AUTH_LOGOUT_URL})
    return HttpResponseRedirect(f"{settings.AUTH_BASE_URL}/logout?{query}")


def admin_login(request):
    if configured() and request.GET.get("local") != "1":
        return HttpResponseRedirect("/auth/login?return_to=/admin/")
    return admin.site.login(request)
