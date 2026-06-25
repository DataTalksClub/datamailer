"""Command-line interface for sending email through a Datamailer deployment."""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from . import __version__, config
from .api import ApiError, DatamailerClient
from .templates import template_for


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    try:
        return args.handler(args)
    except ApiError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datamailer",
        description="Send email through a Datamailer deployment from the terminal.",
    )
    parser.add_argument("--version", action="version", version=f"datamailer {__version__}")

    sub = parser.add_subparsers(dest="command")

    # --- configure --------------------------------------------------------
    p_configure = sub.add_parser("configure", help="Save the deployment URL and API key to the config file.")
    p_configure.add_argument("--url", help="Datamailer base URL, e.g. https://datamailer.example.com")
    p_configure.add_argument("--api-key", dest="api_key", help="Client API key (dm_...).")
    p_configure.add_argument("--default-to", dest="default_to", help="Default recipient for `datamailer send`.")
    p_configure.add_argument("--default-from", dest="default_from", help="Default sender id for `datamailer send`.")
    p_configure.set_defaults(handler=cmd_configure)

    # --- send -------------------------------------------------------------
    p_send = sub.add_parser("send", help="Send an email. Body may come from --body, --body-file, or stdin.")
    _add_connection_args(p_send)
    p_send.add_argument("--to", help="Recipient email (defaults to the configured default_to).")
    p_send.add_argument("--subject", "-s", help="Subject line.")
    p_send.add_argument("--body", "-b", help="Message body. If omitted, read from --body-file or stdin.")
    p_send.add_argument("--body-file", dest="body_file", help="Read the body from this file ('-' for stdin).")
    p_send.add_argument("--html", action="store_true", help="Treat the body as raw HTML instead of plain text.")
    p_send.add_argument("--from", dest="from_", help="Sender id (defaults to the client's default sender).")
    p_send.add_argument("--idempotency-key", dest="idempotency_key", help="Dedupe key; replays return the prior send.")
    p_send.add_argument("--json", action="store_true", help="Print the raw JSON response.")
    p_send.set_defaults(handler=cmd_send)

    # --- status -----------------------------------------------------------
    p_status = sub.add_parser("status", help="Show delivery status for a transactional message id.")
    _add_connection_args(p_status)
    p_status.add_argument("message_id", help="Message id returned by `datamailer send`.")
    p_status.add_argument("--json", action="store_true", help="Print the raw JSON response.")
    p_status.set_defaults(handler=cmd_status)

    # --- whoami -----------------------------------------------------------
    p_whoami = sub.add_parser("whoami", help="Verify the URL + token and show the authenticated client.")
    _add_connection_args(p_whoami)
    p_whoami.add_argument("--json", action="store_true", help="Print the raw JSON response.")
    p_whoami.set_defaults(handler=cmd_whoami)

    # --- senders ----------------------------------------------------------
    p_senders = sub.add_parser("senders", help="Show or set the client's sender addresses.")
    _add_connection_args(p_senders)
    p_senders.add_argument(
        "--set",
        dest="set_senders",
        action="append",
        metavar="id=email",
        help="Define a sender, e.g. --set 'results=Agent <agent@example.com>'. Repeatable.",
    )
    p_senders.add_argument("--default", dest="default_sender", help="Sender id to use as the default.")
    p_senders.add_argument("--json", action="store_true", help="Print the raw JSON response.")
    p_senders.set_defaults(handler=cmd_senders)

    return parser


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", help="Override the configured Datamailer base URL.")
    parser.add_argument("--api-key", dest="api_key", help="Override the configured API key.")


def _client(args) -> DatamailerClient:
    settings = config.resolve(args)
    return DatamailerClient(settings.base_url, settings.api_key)


# --- command handlers -----------------------------------------------------


def cmd_configure(args) -> int:
    url = args.url or _prompt("Datamailer URL", config.resolve(args).url)
    api_key = args.api_key or _prompt("API key (dm_...)", "", secret=True)
    values = {"url": url, "api_key": api_key}
    if args.default_to is not None:
        values["default_to"] = args.default_to
    if args.default_from is not None:
        values["default_from"] = args.default_from

    path = config.save_file(values)
    print(f"Saved configuration to {path}")
    return 0


def cmd_send(args) -> int:
    settings = config.resolve(args)
    client = DatamailerClient(settings.base_url, settings.api_key)

    recipient = args.to or settings.default_to
    if not recipient:
        raise ApiError("No recipient. Pass --to or set a default with `datamailer configure --default-to`.")
    if not args.subject:
        raise ApiError("Missing --subject.")

    body = _read_body(args)
    if not body:
        raise ApiError("Empty body. Pass --body, --body-file, or pipe text on stdin.")

    template_key, template = template_for(args.html)
    payload = {
        "email": recipient,
        "template_key": template_key,
        "context": {"subject": args.subject, "body": body},
    }
    from_sender = args.from_ or settings.default_from
    if from_sender:
        payload["from_email"] = from_sender
    if args.idempotency_key:
        payload["idempotency_key"] = args.idempotency_key

    try:
        result = client.send(payload)
    except ApiError as exc:
        if not exc.is_template_not_found:
            raise
        # First use against this deployment: provision the generic template, then retry once.
        client.upsert_template(template_key, template)
        result = client.send(payload)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    message = result.get("message", {}) if isinstance(result, dict) else {}
    print(f"Sent to {message.get('email', recipient)}")
    print(f"  message id: {message.get('id')}")
    print(f"  status:     {message.get('status')}")
    if result.get("idempotent_replay"):
        print("  (idempotent replay of an earlier send)")
    return 0


def cmd_status(args) -> int:
    result = _client(args).message_status(args.message_id)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    message = result.get("message", {}) if isinstance(result, dict) else {}
    print(f"message {message.get('id')}: {message.get('status')}")
    print(f"  to:      {message.get('email')}")
    print(f"  subject: {message.get('subject')}")
    if message.get("ses_message_id"):
        print(f"  ses id:  {message.get('ses_message_id')}")
    events = result.get("events", []) if isinstance(result, dict) else []
    for event in events:
        print(f"  - {event.get('created_at')}  {event.get('event_type')}")
    return 0


def cmd_whoami(args) -> int:
    result = _client(args).whoami()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    client_info = result.get("client", {}) if isinstance(result, dict) else {}
    print(f"client:  {client_info.get('name')} ({client_info.get('organization')}/{client_info.get('slug')})")
    print(f"default sender: {result.get('default_sender_id') or '(none configured)'}")
    senders = result.get("senders") or []
    if senders:
        print("senders:")
        for sender in senders:
            print(f"  - {sender.get('id')}: {sender.get('email')}")
    return 0


def cmd_senders(args) -> int:
    client = _client(args)
    if not args.set_senders and not args.default_sender:
        result = client.get_senders()
        return _print_senders(result, args.json)

    senders = []
    for item in args.set_senders or []:
        sender_id, sep, email = item.partition("=")
        if not sep or not sender_id.strip() or not email.strip():
            raise ApiError(f"Invalid --set value {item!r}. Use the form id=email.")
        senders.append({"id": sender_id.strip(), "email": email.strip()})

    if not senders:
        raise ApiError("Pass at least one --set id=email to update senders.")

    payload = {"senders": senders}
    payload["default_sender_id"] = args.default_sender or senders[0]["id"]
    result = client.set_senders(payload)
    return _print_senders(result, args.json)


def _print_senders(result, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"default sender: {result.get('default_sender_id') or '(none configured)'}")
    for sender in result.get("senders") or []:
        print(f"  - {sender.get('id')}: {sender.get('email')}")
    return 0


# --- helpers --------------------------------------------------------------


def _read_body(args) -> str:
    if args.body is not None:
        return args.body
    if args.body_file:
        if args.body_file == "-":
            return sys.stdin.read()
        with open(args.body_file, encoding="utf-8") as handle:
            return handle.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _prompt(label: str, default: str, *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    if secret:
        value = getpass.getpass(f"{label}: ")
    else:
        value = input(f"{label}{suffix}: ").strip()
    return value or default


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
