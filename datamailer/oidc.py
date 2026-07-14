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
    return value if value and value.startswith("/") and not value.startswith("//") else "/admin/"


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
    return HttpResponseRedirect(f"{settings.AUTH_BASE_URL}/oauth2/authorize?{query}")


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
        return HttpResponse("Invalid or expired login state", status=400)
    try:
        claims = _verify(_exchange(code, pending["verifier"])["id_token"])
    except Exception:
        return HttpResponse("Login verification failed", status=401)
    if not hmac.compare_digest(str(claims.get("nonce", "")), str(pending.get("nonce", ""))):
        return HttpResponse("Identity token nonce mismatch", status=401)
    email = claims.get("email")
    if not isinstance(email, str) or claims.get("email_verified") is not True:
        return HttpResponse("A verified email address is required", status=401)
    email = email.lower()
    model = get_user_model()
    user = model.objects.filter(email__iexact=email).first()
    if user is None:
        user = model.objects.create_user(username=email, email=email)
    if not user.is_active:
        return HttpResponse("This operator account is disabled", status=403)
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
    return HttpResponseRedirect(_safe_return_to(pending.get("return_to")))


@require_GET
def end(request):
    logout(request)
    if not configured() or not settings.AUTH_LOGOUT_URL:
        return HttpResponseRedirect("/admin/login/")
    query = urllib.parse.urlencode(
        {"client_id": settings.AUTH_CLIENT_ID, "logout_uri": settings.AUTH_LOGOUT_URL}
    )
    return HttpResponseRedirect(f"{settings.AUTH_BASE_URL}/logout?{query}")


def admin_login(request):
    if configured() and request.GET.get("local") != "1":
        return HttpResponseRedirect("/auth/login?return_to=/admin/")
    return admin.site.login(request)
