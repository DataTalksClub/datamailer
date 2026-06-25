import io
import json
import urllib.error

import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Keep tests from reading the developer's real ~/.config/datamailer/config.toml."""
    monkeypatch.setenv("DATAMAILER_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("DATAMAILER_URL", raising=False)
    monkeypatch.delenv("DATAMAILER_API_KEY", raising=False)


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(url, status, body):
    payload = json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(url, status, "error", {}, io.BytesIO(payload))


class FakeServer:
    """A tiny in-memory stand-in for the Datamailer API, routed by (method, path)."""

    def __init__(self):
        self.calls = []
        self.routes = {}

    def route(self, method, path, handler):
        self.routes[(method, path)] = handler

    def urlopen(self, request, timeout=None):
        method = request.method
        path = request.full_url.split("?", 1)[0].split("/api", 1)[-1]
        path = "/api" + path
        payload = json.loads(request.data.decode("utf-8")) if request.data else None
        self.calls.append((method, path, payload))
        handler = self.routes.get((method, path))
        if handler is None:
            raise http_error(request.full_url, 404, {"error": {"code": "not_found"}})
        status, body = handler(payload)
        if status >= 400:
            raise http_error(request.full_url, status, body)
        return FakeResponse(status, body)
