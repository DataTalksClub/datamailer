#!/usr/bin/env python3
import argparse
import html
import importlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path

SELECTED_HEADERS = (
    "list-unsubscribe",
    "list-unsubscribe-post",
    "x-ses-receipt",
    "x-ses-spam-verdict",
    "x-ses-virus-verdict",
    "x-ses-spf-verdict",
    "x-ses-dkim-verdict",
    "x-ses-dmarc-verdict",
)
LINK_RE = re.compile(r"https?://[^\s<>'\")]+", re.IGNORECASE)
HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str


@dataclass
class PartSummary:
    count: int
    characters: int
    preview: str


@dataclass
class EmailSummary:
    subject: str
    from_: str
    to: str
    date: str
    message_id: str
    selected_headers: dict[str, str]
    text: PartSummary
    html: PartSummary
    links: list[str]
    body_text: str
    body_html: str


def pass_(name, detail):
    return CheckResult(name, "PASS", detail)


def warn(name, detail):
    return CheckResult(name, "WARN", detail)


def fail(name, detail):
    return CheckResult(name, "FAIL", detail)


def client_error_code(exc):
    response = getattr(exc, "response", {})
    return response.get("Error", {}).get("Code", exc.__class__.__name__)


def normalize_preview(value, limit=240):
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


def address_values(value):
    return [addr.lower() for _name, addr in getaddresses([value or ""]) if addr]


def html_to_text(value):
    return html.unescape(TAG_RE.sub(" ", value or ""))


def message_parts(message):
    if message.is_multipart():
        return list(message.walk())
    return [message]


def part_text(part):
    try:
        content = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True)
        if payload is None:
            return str(part.get_payload() or "")
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return content if isinstance(content, str) else str(content)


def extract_links(text, html_text, selected_headers):
    links = []
    for value in (text or "", html_text or "", "\n".join(selected_headers.values())):
        links.extend(match.rstrip(".,;") for match in LINK_RE.findall(value))
    links.extend(html.unescape(match).rstrip(".,;") for match in HREF_RE.findall(html_text or ""))

    seen = set()
    deduped = []
    for link in links:
        if link not in seen:
            seen.add(link)
            deduped.append(link)
    return deduped


def summarize_parts(parts):
    joined = "\n".join(parts)
    return PartSummary(count=len(parts), characters=len(joined), preview=normalize_preview(joined))


def parse_mime(raw):
    if not raw.strip():
        raise ValueError("empty MIME input")

    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not message.keys():
        raise ValueError("MIME input has no headers")

    text_parts = []
    html_parts = []
    for part in message_parts(message):
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment":
            continue

        content_type = part.get_content_type()
        if content_type == "text/plain":
            text_parts.append(part_text(part))
        elif content_type == "text/html":
            html_parts.append(part_text(part))

    body_text = "\n".join(text_parts)
    body_html = "\n".join(html_parts)
    selected_headers = {name: str(message[name]) for name in SELECTED_HEADERS if message[name] is not None}

    return EmailSummary(
        subject=str(message["subject"] or ""),
        from_=str(message["from"] or ""),
        to=str(message["to"] or ""),
        date=str(message["date"] or ""),
        message_id=str(message["message-id"] or ""),
        selected_headers=selected_headers,
        text=summarize_parts(text_parts),
        html=summarize_parts(html_parts),
        links=extract_links(body_text, body_html, selected_headers),
        body_text=body_text,
        body_html=body_html,
    )


def summary_results(summary, source):
    results = [
        pass_("Message source", source),
        pass_("Subject", summary.subject or "(missing)"),
        pass_("From", summary.from_ or "(missing)"),
        pass_("To", summary.to or "(missing)"),
        pass_("Date", summary.date or "(missing)"),
        pass_("Message-ID", summary.message_id or "(missing)"),
    ]

    if summary.selected_headers:
        for name, value in summary.selected_headers.items():
            results.append(pass_(f"Header {name}", normalize_preview(value)))
    else:
        results.append(warn("Selected headers", "none found"))

    text_detail = (
        f"parts={summary.text.count} chars={summary.text.characters} preview={summary.text.preview or '(empty)'}"
    )
    html_detail = (
        f"parts={summary.html.count} chars={summary.html.characters} preview={summary.html.preview or '(empty)'}"
    )
    results.append(
        pass_("Text part summary", text_detail) if summary.text.count else warn("Text part summary", text_detail)
    )
    results.append(
        pass_("HTML part summary", html_detail) if summary.html.count else warn("HTML part summary", html_detail)
    )

    if summary.links:
        for link in summary.links:
            results.append(pass_("Detected link", link))
    else:
        results.append(warn("Detected links", "none found"))
    return results


def assertion_results(summary, args):
    results = []
    recipients = address_values(summary.to)
    senders = address_values(summary.from_)
    combined_body = "\n".join([summary.body_text, html_to_text(summary.body_html), summary.body_html])
    link_blob = "\n".join(summary.links)
    selected_header_blob = "\n".join(summary.selected_headers.values())

    for expected in args.expect_to:
        if expected.lower() in recipients:
            results.append(pass_("Expected recipient", expected))
        else:
            results.append(fail("Expected recipient", f"expected={expected} actual={summary.to or '(missing)'}"))

    for expected in args.expect_from:
        if expected.lower() in senders:
            results.append(pass_("Expected sender", expected))
        else:
            results.append(fail("Expected sender", f"expected={expected} actual={summary.from_ or '(missing)'}"))

    for expected in args.expect_subject:
        if expected.lower() in summary.subject.lower():
            results.append(pass_("Expected subject substring", expected))
        else:
            results.append(
                fail("Expected subject substring", f"missing={expected} actual={summary.subject or '(missing)'}")
            )

    for expected in args.expect_body:
        if expected.lower() in combined_body.lower():
            results.append(pass_("Expected body substring", expected))
        else:
            results.append(fail("Expected body substring", f"missing={expected}"))

    if args.expect_unsubscribe_link:
        unsubscribe_blob = "\n".join([link_blob, selected_header_blob])
        if "unsubscribe" in unsubscribe_blob.lower():
            results.append(pass_("Unsubscribe link", "present"))
        else:
            results.append(fail("Unsubscribe link", "missing unsubscribe URL or List-Unsubscribe header"))

    for expected in args.expect_tracking_substring:
        haystack = "\n".join([link_blob, selected_header_blob, combined_body])
        if expected.lower() in haystack.lower():
            results.append(pass_("Expected tracking substring", expected))
        else:
            results.append(fail("Expected tracking substring", f"missing={expected}"))

    return results


def load_terraform_outputs(terraform_dir="", terraform_output_json=""):
    if terraform_output_json:
        raw = Path(terraform_output_json).read_text()
    elif terraform_dir:
        completed = subprocess.run(
            ["terraform", f"-chdir={terraform_dir}", "output", "-json"],
            check=True,
            capture_output=True,
            text=True,
        )
        raw = completed.stdout
    else:
        return {}

    outputs = json.loads(raw)
    return {name: item.get("value") for name, item in outputs.items()}


def inbound_config_from_sources(args):
    outputs = load_terraform_outputs(args.terraform_dir, args.terraform_output_json)
    inbound_mail = outputs.get("inbound_mail") or {}
    return {
        "region": args.region or os.environ.get("AWS_REGION") or outputs.get("aws_region") or "us-east-1",
        "bucket": args.inbound_bucket or os.environ.get("INBOUND_MAIL_BUCKET") or inbound_mail.get("bucket") or "",
        "prefix": args.inbound_prefix or os.environ.get("INBOUND_MAIL_PREFIX") or inbound_mail.get("s3_prefix") or "",
    }


def list_recent_s3_objects(s3, bucket, prefix, max_keys):
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=max_keys)
    objects = response.get("Contents", [])
    return sorted(objects, key=lambda item: item.get("LastModified"), reverse=True)


def read_s3_object(s3, bucket, key):
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    return body if isinstance(body, bytes) else body.encode()


def read_input(args, session=None):
    if args.fixture:
        path = Path(args.fixture)
        return path.read_bytes(), f"fixture={path}"

    config = inbound_config_from_sources(args)
    if not config["bucket"]:
        raise RuntimeError(
            "missing inbound mail bucket; pass --inbound-bucket or Terraform output with inbound_mail.bucket"
        )

    if session is None:
        boto3 = importlib.import_module("boto3")
        session = boto3.Session(region_name=config["region"])
    s3 = session.client("s3", region_name=config["region"])

    if args.latest:
        objects = list_recent_s3_objects(s3, config["bucket"], config["prefix"], args.max_keys)
        if not objects:
            raise RuntimeError(f"no inbound S3 objects found under s3://{config['bucket']}/{config['prefix']}")
        key = objects[0]["Key"]
    else:
        key = args.s3_key

    if not key:
        raise RuntimeError("missing S3 key; pass --latest or --s3-key")
    return read_s3_object(s3, config["bucket"], key), f"s3://{config['bucket']}/{key}"


def build_parser():
    parser = argparse.ArgumentParser(description="Inspect raw inbound MIME mail from a fixture or SES inbound S3.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", default="", help="Path to a local .eml fixture.")
    source.add_argument("--latest", action="store_true", help="Inspect the latest S3 object under the inbound prefix.")
    source.add_argument("--s3-key", default="", help="Inspect an explicit inbound S3 object key.")

    parser.add_argument("--terraform-dir", default="", help="Terraform root to read with `terraform output -json`.")
    parser.add_argument("--terraform-output-json", default="", help="Path to a saved `terraform output -json` file.")
    parser.add_argument(
        "--region", default="", help="AWS region. Defaults to AWS_REGION, Terraform output, then us-east-1."
    )
    parser.add_argument("--inbound-bucket", default="", help="Inbound mail S3 bucket.")
    parser.add_argument("--inbound-prefix", default="", help="Inbound mail S3 prefix.")
    parser.add_argument("--max-keys", type=int, default=20, help="Maximum S3 objects to list before selecting latest.")

    parser.add_argument("--expect-to", action="append", default=[], help="Expected recipient email; may be repeated.")
    parser.add_argument("--expect-from", action="append", default=[], help="Expected sender email; may be repeated.")
    parser.add_argument(
        "--expect-subject", action="append", default=[], help="Required subject substring; may be repeated."
    )
    parser.add_argument("--expect-body", action="append", default=[], help="Required body substring; may be repeated.")
    parser.add_argument(
        "--expect-unsubscribe-link", action="store_true", help="Require an unsubscribe link or List-Unsubscribe header."
    )
    parser.add_argument(
        "--expect-tracking-substring",
        action="append",
        default=[],
        help="Required tracking/click/open host or URL substring; may be repeated.",
    )
    return parser


def main(argv=None, session=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    results = []
    try:
        raw, source = read_input(args, session=session)
        summary = parse_mime(raw)
        results.extend(summary_results(summary, source))
        results.extend(assertion_results(summary, args))
    except Exception as exc:
        results.append(fail("Inbound MIME inspection", f"{client_error_code(exc)}: {exc}"))

    failed = False
    for result in results:
        print(f"{result.status}: {result.name}: {result.detail}")
        failed = failed or result.status == "FAIL"
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
