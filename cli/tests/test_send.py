import urllib.request
from argparse import Namespace

import pytest
from datamailer_cli import api, cli
from datamailer_cli.templates import TEXT_TEMPLATE_KEY

from conftest import FakeServer


@pytest.fixture
def server(monkeypatch):
    fake = FakeServer()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


def _send_args(**overrides):
    args = Namespace(
        url="https://x.example.com",
        api_key="dm_test",
        to="me@example.com",
        subject="Results",
        body="all done",
        body_file=None,
        html=False,
        from_=None,
        idempotency_key=None,
        json=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_send_provisions_template_on_first_use(server, capsys):
    state = {"provisioned": False}

    def send_handler(payload):
        if not state["provisioned"]:
            return 404, {"error": {"code": "validation_error", "fields": {"template_key": "not_found"}}}
        return 202, {"message": {"id": 7, "email": payload["email"], "status": "queued"}, "idempotent_replay": False}

    def template_handler(payload):
        state["provisioned"] = True
        return 200, {"template": {"key": TEXT_TEMPLATE_KEY}}

    server.route("POST", "/api/transactional/send", send_handler)
    server.route("PUT", f"/api/transactional/templates/{TEXT_TEMPLATE_KEY}", template_handler)

    assert cli.cmd_send(_send_args()) == 0

    methods = [(method, path) for method, path, _ in server.calls]
    assert methods == [
        ("POST", "/api/transactional/send"),
        ("PUT", f"/api/transactional/templates/{TEXT_TEMPLATE_KEY}"),
        ("POST", "/api/transactional/send"),
    ]
    out = capsys.readouterr().out
    assert "message id: 7" in out
    assert "status:     queued" in out


def test_send_passes_subject_and_body_as_context(server):
    server.route(
        "POST",
        "/api/transactional/send",
        lambda payload: (202, {"message": {"id": 1, "email": payload["email"], "status": "queued"}}),
    )
    cli.cmd_send(_send_args(subject="Hi", body="line1\nline2"))
    _, _, payload = server.calls[0]
    assert payload["context"] == {"subject": "Hi", "body": "line1\nline2"}
    assert payload["template_key"] == TEXT_TEMPLATE_KEY


def test_send_requires_recipient(server):
    with pytest.raises(api.ApiError):
        cli.cmd_send(_send_args(to=None))


def test_send_surfaces_suppression_error(server):
    server.route(
        "POST",
        "/api/transactional/send",
        lambda payload: (409, {"error": {"code": "transactional_suppressed", "message": "blocked"}}),
    )
    with pytest.raises(api.ApiError) as excinfo:
        cli.cmd_send(_send_args())
    assert excinfo.value.code == "transactional_suppressed"
