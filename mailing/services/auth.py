import secrets
from dataclasses import dataclass

from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone

from mailing.models import Client, ClientApiKey

API_KEY_PREFIX = "dm_"


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


def generate_raw_api_key(public_id=None):
    public_id = public_id or secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    secret = secrets.token_urlsafe(32)
    return f"{API_KEY_PREFIX}{public_id}_{secret}"


def public_id_from_raw_key(raw_api_key):
    if not raw_api_key.startswith(API_KEY_PREFIX):
        return ""
    remainder = raw_api_key[len(API_KEY_PREFIX) :]
    public_id, separator, _secret = remainder.partition("_")
    if not separator or not public_id:
        return ""
    return public_id


def create_client_api_key(*, client, name, notes="", raw_api_key=None, public_id=None):
    raw_key = raw_api_key or generate_raw_api_key(public_id=public_id)
    parsed_public_id = public_id or public_id_from_raw_key(raw_key)
    if not parsed_public_id:
        parsed_public_id = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    api_key = ClientApiKey.objects.create(
        client=client,
        name=name,
        notes=notes,
        public_id=parsed_public_id,
        key_hash=hash_api_key(raw_key),
    )
    return api_key, raw_key


def authenticate_bearer_token(authorization_header):
    if not authorization_header:
        return AuthResult(client=None, error="missing_authorization")

    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return AuthResult(client=None, error="invalid_authorization")

    raw_api_key = token.strip()
    public_id = public_id_from_raw_key(raw_api_key)
    api_keys = ClientApiKey.objects.select_related("client", "client__organization").filter(revoked_at__isnull=True)
    if public_id:
        api_keys = api_keys.filter(public_id=public_id)

    for api_key in api_keys:
        if check_api_key(raw_api_key, api_key.key_hash):
            if not api_key.client.is_active:
                return AuthResult(client=None, error="inactive_client")
            api_key.last_used_at = timezone.now()
            api_key.save(update_fields=["last_used_at", "updated_at"])
            return AuthResult(client=api_key.client)

    return AuthResult(client=None, error="invalid_api_key")
