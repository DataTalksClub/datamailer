from dataclasses import dataclass

from django.contrib.auth.hashers import check_password, make_password

from mailing.models import Client


@dataclass(frozen=True)
class AuthResult:
    client: Client | None
    error: str | None = None
    status_code: int = 401

    @property
    def is_authenticated(self):
        return self.client is not None


def hash_api_key(raw_api_key):
    return make_password(raw_api_key)


def check_api_key(raw_api_key, api_key_hash):
    if not raw_api_key or not api_key_hash:
        return False
    return check_password(raw_api_key, api_key_hash)


def authenticate_bearer_token(authorization_header):
    if not authorization_header:
        return AuthResult(client=None, error="missing_authorization")

    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return AuthResult(client=None, error="invalid_authorization")

    raw_api_key = token.strip()
    for client in Client.objects.select_related("organization").exclude(api_key_hash=""):
        if check_api_key(raw_api_key, client.api_key_hash):
            if not client.is_active:
                return AuthResult(client=None, error="inactive_client")
            return AuthResult(client=client)

    return AuthResult(client=None, error="invalid_api_key")
