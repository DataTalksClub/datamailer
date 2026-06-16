#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

TEMPLATE_KEY = "homework-submission-confirmation"

TEMPLATE_PAYLOAD = {
    "name": "Homework Submission Confirmation",
    "description": "Confirm that the course platform saved a homework submission.",
    "subject": "Homework submission received: {{ homework_title }}",
    "html_body": (
        "<p>Your homework submission for <strong>{{ homework_title }}</strong> "
        "in {{ course_title }} was saved.</p>"
        "<p>You can update it until the homework closes.</p>"
    ),
    "text_body": (
        "Your homework submission for {{ homework_title }} in "
        "{{ course_title }} was saved.\n\n"
        "You can update it until the homework closes."
    ),
    "required_context": [
        {"name": "course_title", "description": "Course title."},
        {"name": "homework_title", "description": "Homework title."},
        {"name": "submission_id", "description": "Course platform submission id."},
        {"name": "submitted_at", "description": "Submission timestamp."},
    ],
    "example_context": {
        "course_slug": "ml-zoomcamp",
        "course_title": "ML Zoomcamp",
        "homework_slug": "homework-1",
        "homework_title": "Homework 1",
        "submission_id": 123,
        "submitted_at": "2026-06-16T12:00:00+00:00",
    },
    "is_active": True,
}


def request_json(method, url, api_key, payload=None):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {url} failed with HTTP {exc.code}: {detail}") from exc


def main():
    parser = argparse.ArgumentParser(
        description="Create or update the CMP homework confirmation transactional template via Datamailer API."
    )
    parser.add_argument("--base-url", default=os.environ.get("DATAMAILER_URL", "http://127.0.0.1:8001"))
    parser.add_argument("--api-key", default=os.environ.get("DATAMAILER_API_KEY", ""))
    args = parser.parse_args()

    if not args.api_key:
        print("DATAMAILER_API_KEY or --api-key is required.", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    url = f"{base_url}/api/transactional/templates/{TEMPLATE_KEY}"
    status, payload = request_json("PUT", url, args.api_key, TEMPLATE_PAYLOAD)

    print(json.dumps({"status": status, **payload}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
