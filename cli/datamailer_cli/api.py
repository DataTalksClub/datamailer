"""Minimal, dependency-free HTTP client for the Datamailer API.

Uses only the standard library (``urllib``) so the CLI installs fast and light.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import __version__


class ApiError(Exception):
    """A non-2xx response (or transport failure) from the Datamailer API."""

    def __init__(self, message: str, *, status: int | None = None, code: str = "", fields: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.fields = fields or {}

    @property
    def is_template_not_found(self) -> bool:
        return self.status == 404 and self.fields.get("template_key") == "not_found"


class DatamailerClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0):
        if not base_url:
            raise ApiError("No Datamailer URL configured. Run `datamailer configure` or set DATAMAILER_URL.")
        if not api_key:
            raise ApiError("No API key configured. Run `datamailer configure` or set DATAMAILER_API_KEY.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def request(self, method: str, path: str, *, payload: Any = None, query: dict | None = None) -> tuple[int, Any]:
        url = self.base_url + path
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"datamailer-cli/{__version__}",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, _parse_body(response.read())
        except urllib.error.HTTPError as exc:
            raise _error_from_response(exc.code, exc.read()) from exc
        except urllib.error.URLError as exc:
            raise ApiError(f"Could not reach Datamailer at {self.base_url}: {exc.reason}") from exc

    # --- endpoint helpers -------------------------------------------------

    def whoami(self) -> Any:
        _, data = self.request("GET", "/api/client/senders")
        return data

    def get_senders(self) -> Any:
        _, data = self.request("GET", "/api/client/senders")
        return data

    def set_senders(self, payload: dict) -> Any:
        _, data = self.request("PUT", "/api/client/senders", payload=payload)
        return data

    def upsert_template(self, template_key: str, payload: dict) -> Any:
        _, data = self.request("PUT", f"/api/transactional/templates/{template_key}", payload=payload)
        return data

    def send(self, payload: dict) -> Any:
        _, data = self.request("POST", "/api/transactional/send", payload=payload)
        return data

    def message_status(self, message_id: int | str) -> Any:
        _, data = self.request("GET", f"/api/transactional/messages/{message_id}")
        return data


def _parse_body(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"raw": raw.decode("utf-8", errors="replace")}


def _error_from_response(status: int, raw: bytes) -> ApiError:
    data = _parse_body(raw)
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code", ""))
        fields = error.get("fields") if isinstance(error.get("fields"), dict) else {}
        message = error.get("message") or _message_for(code, fields, status)
        return ApiError(message, status=status, code=code, fields=fields)
    return ApiError(f"Request failed with HTTP {status}.", status=status)


def _message_for(code: str, fields: dict, status: int) -> str:
    if code == "validation_error" and fields:
        details = ", ".join(f"{name}: {reason}" for name, reason in fields.items())
        return f"Validation failed ({details})."
    if code:
        return f"{code} (HTTP {status})."
    return f"Request failed with HTTP {status}."
