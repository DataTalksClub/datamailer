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
        "<p>{{ intro_text|default:'Your homework submission was saved.' }}</p>"
        "<p>You can update it while the homework is open: "
        "<a href=\"{{ update_url }}\">{{ update_url }}</a></p>"
        "{% if submitted_answers %}"
        "<h2>Submitted answers</h2>"
        "<ol>"
        "{% for answer in submitted_answers %}"
        "<li>{{ answer.question }}: "
        "{% if answer.answer %}{{ answer.answer|linebreaksbr }}{% else %}Not submitted{% endif %}</li>"
        "{% endfor %}"
        "</ol>"
        "{% endif %}"
        "{% if submission_fields %}"
        "<h2>Submitted details</h2>"
        "<ul>"
        "{% for field in submission_fields %}"
        "<li>{{ field.label }}: "
        "{% if field.value %}{{ field.value|linebreaksbr }}{% else %}Not submitted{% endif %}</li>"
        "{% endfor %}"
        "</ul>"
        "{% endif %}"
        "{% if notification_footer or profile_url %}"
        "<hr>"
        "<p style=\"color:#57606a;font-size:13px;line-height:1.5\">"
        "{% if notification_footer %}{{ notification_footer }} {% endif %}"
        "If you don't want to receive these emails, you can "
        "{% if profile_url %}"
        "<a href=\"{{ profile_url }}\">turn them off in your profile</a>."
        "{% else %}"
        "turn them off in your profile."
        "{% endif %}"
        "</p>"
        "{% endif %}"
    ),
    "text_body": (
        "{{ intro_text|default:'Your homework submission was saved.' }}\n\n"
        "Update your submission: {{ update_url }}\n\n"
        "{% if submitted_answers_text %}"
        "Submitted answers:\n{{ submitted_answers_text }}\n\n"
        "{% endif %}"
        "{% if submitted_fields_text %}"
        "Submitted details:\n{{ submitted_fields_text }}\n\n"
        "{% endif %}"
        "{% if notification_footer_text %}"
        "{{ notification_footer_text }}\n"
        "{% elif notification_footer %}"
        "{{ notification_footer }}\n"
        "{% if profile_url %}"
        "Manage email preferences: {{ profile_url }}\n"
        "{% endif %}"
        "{% endif %}"
    ),
    "required_context": [
        {"name": "course_title", "description": "Course title."},
        {"name": "homework_title", "description": "Homework title."},
        {"name": "submission_id", "description": "Course platform submission id."},
        {"name": "submitted_at", "description": "Submission timestamp."},
        {"name": "update_url", "description": "Absolute URL where the learner can update the submission."},
        {"name": "profile_url", "description": "Absolute URL where the learner can manage email preferences."},
    ],
    "example_context": {
        "course_slug": "ml-zoomcamp",
        "course_title": "ML Zoomcamp",
        "homework_slug": "homework-1",
        "homework_title": "Homework 1",
        "submission_id": 123,
        "submitted_at": "2026-06-16T12:00:00+00:00",
        "update_url": "https://courses.datatalks.club/ml-zoomcamp/homework/homework-1",
        "intro_text": "Your homework submission for Homework 1 in ML Zoomcamp was saved.",
        "submission_fields": [
            {
                "key": "homework_url",
                "label": "Homework URL",
                "value": "https://github.com/example/homework",
            },
            {
                "key": "time_spent_homework",
                "label": "Time spent on homework",
                "value": "4 hours",
            },
        ],
        "submitted_answers": [
            {
                "question_id": 1,
                "question": "Pick one option",
                "question_type": "MC",
                "answer": "2. Second option",
                "raw_answer": "2",
                "selected_options": [{"index": 2, "value": "Second option"}],
            }
        ],
        "submitted_fields_text": "Homework URL: https://github.com/example/homework\nTime spent on homework: 4 hours",
        "submitted_answers_text": "Pick one option: 2. Second option",
        "profile_url": "https://courses.datatalks.club/accounts/settings/",
        "notification_footer": (
            "You are receiving this because homework and project submission "
            "emails are enabled in your profile."
        ),
        "notification_footer_text": (
            "If you don't want to receive these emails, you can turn off "
            "homework and project submission emails in your profile: "
            "https://courses.datatalks.club/accounts/settings/"
        ),
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
