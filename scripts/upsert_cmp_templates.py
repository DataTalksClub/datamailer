#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SUBMISSION_HTML = (
    "<p>{{ intro_text }}</p>"
    "<p>{{ update_text }} "
    "<a href=\"{{ update_url }}\">{{ update_link_text|default:'Update submission' }}</a></p>"
    "{% if submitted_answers %}"
    "<h2>Submitted answers</h2><ol>"
    "{% for answer in submitted_answers %}"
    "<li>{{ answer.question }}: "
    "{% if answer.answer %}{{ answer.answer|linebreaksbr }}{% else %}Not submitted{% endif %}</li>"
    "{% endfor %}</ol>"
    "{% endif %}"
    "{% if submission_fields %}"
    "<h2>Submitted details</h2><ul>"
    "{% for field in submission_fields %}"
    "<li>{{ field.label }}: "
    "{% if field.value %}{{ field.value|linebreaksbr }}{% else %}Not submitted{% endif %}</li>"
    "{% endfor %}</ul>"
    "{% endif %}"
    '<hr><p style="color:#57606a;font-size:13px;line-height:1.5">'
    "{{ notification_footer }} "
    'Manage preferences: <a href="{{ profile_url }}">{{ profile_url }}</a>'
    "</p>"
)

SUBMISSION_TEXT = (
    "{{ intro_text }}\n\n"
    "{{ update_text }}\n\n"
    "{% if submitted_answers_text %}"
    "Submitted answers:\n{{ submitted_answers_text }}\n\n"
    "{% endif %}"
    "{% if submitted_fields_text %}"
    "Submitted details:\n{{ submitted_fields_text }}\n\n"
    "{% endif %}"
    "{{ notification_footer_text }}\n"
)


TEMPLATES = {
    "homework-submission-confirmation": {
        "name": "Homework Submission Confirmation",
        "description": "Confirm that CMP saved a homework submission.",
        "subject": "{{ email_subject }}",
        "html_body": SUBMISSION_HTML,
        "text_body": SUBMISSION_TEXT,
        "required_context": [
            {"name": "course_title", "description": "Course title."},
            {"name": "homework_title", "description": "Homework title."},
            {"name": "submission_id", "description": "CMP submission id."},
            {"name": "submitted_at", "description": "Submission timestamp."},
            {"name": "update_url", "description": "Submission update URL."},
            {"name": "profile_url", "description": "Preference settings URL."},
            {"name": "intro_text", "description": "Opening sentence."},
            {"name": "notification_footer", "description": "Preference footer."},
            {"name": "notification_footer_text", "description": "Plain-text preference footer."},
        ],
        "example_context": {
            "email_subject": "Homework submission saved: Homework 1",
            "course_title": "ML Zoomcamp",
            "homework_title": "Homework 1",
            "submission_id": 123,
            "submitted_at": "2026-06-16T12:00:00+00:00",
            "update_url": "https://courses.datatalks.club/ml-zoomcamp/homework/homework-1",
            "profile_url": "https://courses.datatalks.club/accounts/settings/",
            "intro_text": "Your homework submission for Homework 1 in ML Zoomcamp was saved.",
            "update_text": "You can update your submission while the homework is open.",
            "update_link_text": "Update your submission",
            "submission_fields": [
                {
                    "key": "time_spent_homework",
                    "label": "Time spent on homework",
                    "value": "4 hours",
                }
            ],
            "submitted_answers": [
                {
                    "question": "Pick one option",
                    "answer": "2. Second option",
                }
            ],
            "submitted_fields_text": "Time spent on homework: 4 hours",
            "submitted_answers_text": "Pick one option: 2. Second option",
            "notification_footer": "You are receiving this because homework and project submission emails are enabled.",
            "notification_footer_text": "Manage preferences: https://courses.datatalks.club/accounts/settings/",
        },
        "is_active": True,
    },
    "project-submission-confirmation": {
        "name": "Project Submission Confirmation",
        "description": "Confirm that CMP saved a project submission.",
        "subject": "{{ email_subject }}",
        "html_body": SUBMISSION_HTML,
        "text_body": SUBMISSION_TEXT,
        "required_context": [
            {"name": "course_title", "description": "Course title."},
            {"name": "project_title", "description": "Project title."},
            {"name": "submission_id", "description": "CMP submission id."},
            {"name": "submitted_at", "description": "Submission timestamp."},
            {"name": "update_url", "description": "Submission update URL."},
            {"name": "profile_url", "description": "Preference settings URL."},
            {"name": "intro_text", "description": "Opening sentence."},
            {"name": "notification_footer", "description": "Preference footer."},
            {"name": "notification_footer_text", "description": "Plain-text preference footer."},
        ],
        "example_context": {
            "email_subject": "Project submission saved: Midterm Project",
            "course_title": "ML Zoomcamp",
            "project_title": "Midterm Project",
            "submission_id": 456,
            "submitted_at": "2026-06-16T12:00:00+00:00",
            "update_url": "https://courses.datatalks.club/ml-zoomcamp/project/midterm",
            "profile_url": "https://courses.datatalks.club/accounts/settings/",
            "intro_text": "Your project submission for Midterm Project in ML Zoomcamp was saved.",
            "update_text": "You can update your submission while the project is open.",
            "update_link_text": "Update your submission",
            "submission_fields": [
                {
                    "key": "github_link",
                    "label": "GitHub repository",
                    "value": "https://github.com/example/project",
                },
                {"key": "commit_id", "label": "Commit ID", "value": "abc123"},
            ],
            "submitted_fields_text": "GitHub repository: https://github.com/example/project\nCommit ID: abc123",
            "notification_footer": "You are receiving this because homework and project submission emails are enabled.",
            "notification_footer_text": "Manage preferences: https://courses.datatalks.club/accounts/settings/",
        },
        "is_active": True,
    },
    "homework-score-notification": {
        "name": "Homework Score Notification",
        "description": "Tell homework submitters that scores are available.",
        "subject": "Scores available: {{ homework_title }}",
        "html_body": (
            "<p>Your score for <strong>{{ homework_title }}</strong> in {{ course_title }} is ready.</p>"
            "<p>Your score: <strong>{{ total_score }}</strong></p>"
            "<ul>"
            "<li>Questions: {{ questions_score }}</li>"
            "<li>Learning in public: {{ learning_in_public_score }}</li>"
            "<li>FAQ contribution: {{ faq_score }}</li>"
            "</ul>"
            "<h2>Next steps</h2>"
            "<ul>"
            '<li><a href="{{ scores_url }}">Review your homework score</a></li>'
            '<li><a href="{{ leaderboard_url }}">Check the course leaderboard</a></li>'
            '<li><a href="{{ course_url }}">Open the course page</a></li>'
            "</ul>"
            '<hr><p style="color:#57606a;font-size:13px;line-height:1.5">'
            "{{ notification_footer }} "
            'To stop receiving these emails, update your profile settings: '
            '<a href="{{ profile_url }}">{{ profile_url }}</a></p>'
        ),
        "text_body": (
            "Your score for {{ homework_title }} in {{ course_title }} is ready.\n\n"
            "Your score: {{ total_score }}\n"
            "Breakdown:\n"
            "- Questions: {{ questions_score }}\n"
            "- Learning in public: {{ learning_in_public_score }}\n"
            "- FAQ contribution: {{ faq_score }}\n\n"
            "Next steps:\n"
            "- Review your homework score: {{ scores_url }}\n"
            "- Check the course leaderboard: {{ leaderboard_url }}\n"
            "- Open the course page: {{ course_url }}\n\n"
            "{{ notification_footer_text }}\n"
        ),
        "required_context": [
            {"name": "course_title", "description": "Course title."},
            {"name": "homework_title", "description": "Homework title."},
            {"name": "course_url", "description": "Course page URL."},
            {"name": "questions_score", "description": "Homework question score for this learner."},
            {"name": "learning_in_public_score", "description": "Learning in public score for this learner."},
            {"name": "faq_score", "description": "FAQ contribution score for this learner."},
            {"name": "total_score", "description": "Total homework score for this learner."},
            {"name": "scores_url", "description": "URL where scores can be viewed."},
            {"name": "leaderboard_url", "description": "Course leaderboard URL."},
            {"name": "profile_url", "description": "Preference settings URL."},
            {"name": "notification_footer", "description": "Preference footer."},
            {"name": "notification_footer_text", "description": "Plain-text preference footer."},
        ],
        "example_context": {
            "course_title": "ML Zoomcamp",
            "homework_title": "Homework 1",
            "course_url": "https://courses.datatalks.club/ml-zoomcamp/",
            "questions_score": 6,
            "learning_in_public_score": 2,
            "faq_score": 1,
            "total_score": 9,
            "scores_url": "https://courses.datatalks.club/ml-zoomcamp/homework/homework-1",
            "leaderboard_url": "https://courses.datatalks.club/ml-zoomcamp/leaderboard",
            "profile_url": "https://courses.datatalks.club/accounts/settings/",
            "notification_footer": (
                "You are receiving this because you submitted Homework 1 for ML Zoomcamp and "
                "homework/project submission emails are enabled in your profile."
            ),
            "notification_footer_text": (
                "If you don't want to receive homework/project submission and score emails, "
                "turn off homework and project submission emails in your profile: "
                "https://courses.datatalks.club/accounts/settings/"
            ),
        },
        "is_active": True,
    },
    "project-score-notification": {
        "name": "Project Score Notification",
        "description": "Tell project submitters that scores are available.",
        "subject": "Scores available: {{ project_title }}",
        "html_body": (
            "<p>Your score for <strong>{{ project_title }}</strong> in {{ course_title }} is ready.</p>"
            "<p>Your score: <strong>{{ total_score }}</strong></p>"
            "<ul>"
            "<li>Project: {{ project_score }}</li>"
            "<li>Project learning in public: {{ project_learning_in_public_score }}</li>"
            "<li>Project FAQ: {{ project_faq_score }}</li>"
            "<li>Peer review: {{ peer_review_score }}</li>"
            "<li>Peer review learning in public: {{ peer_review_learning_in_public_score }}</li>"
            "</ul>"
            "{% if github_link %}"
            "<p>Submission reviewed: "
            '<a href="{{ github_link }}">GitHub repository</a>'
            "{% if commit_id %} at commit <code>{{ commit_id }}</code>{% endif %}.</p>"
            "{% endif %}"
            "<h2>Next steps</h2>"
            "<ul>"
            '<li><a href="{{ scores_url }}">Review your project result</a></li>'
            '<li><a href="{{ project_url }}">Open the project page</a></li>'
            '<li><a href="{{ leaderboard_url }}">Check the course leaderboard</a></li>'
            '<li><a href="{{ course_url }}">Open the course page</a></li>'
            "</ul>"
            '<hr><p style="color:#57606a;font-size:13px;line-height:1.5">'
            "{{ notification_footer }} "
            'To stop receiving these emails, update your profile settings: '
            '<a href="{{ profile_url }}">{{ profile_url }}</a></p>'
        ),
        "text_body": (
            "Your score for {{ project_title }} in {{ course_title }} is ready.\n\n"
            "Your score: {{ total_score }}\n"
            "Breakdown:\n"
            "- Project: {{ project_score }}\n"
            "- Project learning in public: {{ project_learning_in_public_score }}\n"
            "- Project FAQ: {{ project_faq_score }}\n"
            "- Peer review: {{ peer_review_score }}\n"
            "- Peer review learning in public: {{ peer_review_learning_in_public_score }}\n\n"
            "{% if github_link %}"
            "Submission reviewed: {{ github_link }}{% if commit_id %} at commit {{ commit_id }}{% endif %}\n\n"
            "{% endif %}"
            "Next steps:\n"
            "- Review your project result: {{ scores_url }}\n"
            "- Open the project page: {{ project_url }}\n"
            "- Check the course leaderboard: {{ leaderboard_url }}\n"
            "- Open the course page: {{ course_url }}\n\n"
            "{{ notification_footer_text }}\n"
        ),
        "required_context": [
            {"name": "course_title", "description": "Course title."},
            {"name": "project_title", "description": "Project title."},
            {"name": "course_url", "description": "Course page URL."},
            {"name": "project_url", "description": "Project page URL."},
            {"name": "project_score", "description": "Project score for this learner."},
            {
                "name": "project_learning_in_public_score",
                "description": "Project learning in public score for this learner.",
            },
            {"name": "project_faq_score", "description": "Project FAQ contribution score for this learner."},
            {"name": "peer_review_score", "description": "Peer review score for this learner."},
            {
                "name": "peer_review_learning_in_public_score",
                "description": "Peer review learning in public score for this learner.",
            },
            {"name": "total_score", "description": "Total project score for this learner."},
            {"name": "scores_url", "description": "URL where scores can be viewed."},
            {"name": "leaderboard_url", "description": "Course leaderboard URL."},
            {"name": "profile_url", "description": "Preference settings URL."},
            {"name": "notification_footer", "description": "Preference footer."},
            {"name": "notification_footer_text", "description": "Plain-text preference footer."},
        ],
        "example_context": {
            "course_title": "ML Zoomcamp",
            "project_title": "Midterm Project",
            "course_url": "https://courses.datatalks.club/ml-zoomcamp/",
            "project_url": "https://courses.datatalks.club/ml-zoomcamp/project/midterm",
            "project_score": 70,
            "project_learning_in_public_score": 5,
            "project_faq_score": 1,
            "peer_review_score": 18,
            "peer_review_learning_in_public_score": 4,
            "total_score": 98,
            "github_link": "https://github.com/example/project",
            "commit_id": "abc123",
            "scores_url": "https://courses.datatalks.club/ml-zoomcamp/project/midterm/results",
            "leaderboard_url": "https://courses.datatalks.club/ml-zoomcamp/leaderboard",
            "profile_url": "https://courses.datatalks.club/accounts/settings/",
            "notification_footer": (
                "You are receiving this because you submitted Midterm Project for ML Zoomcamp and "
                "homework/project submission emails are enabled in your profile."
            ),
            "notification_footer_text": (
                "If you don't want to receive homework/project submission and score emails, "
                "turn off homework and project submission emails in your profile: "
                "https://courses.datatalks.club/accounts/settings/"
            ),
        },
        "is_active": True,
    },
    "certificate-availability-notification": {
        "name": "Certificate Availability Notification",
        "description": "Tell learners that a course certificate is available.",
        "subject": "{{ email_subject }}",
        "html_body": (
            "<p>{{ intro_text }}</p>"
            '<p><a href="{{ certificate_url }}">Download certificate</a></p>'
            '<p>Course page: <a href="{{ course_url }}">{{ course_url }}</a></p>'
            '<hr><p style="color:#57606a;font-size:13px;line-height:1.5">'
            "{{ notification_footer }} Manage preferences: "
            '<a href="{{ profile_url }}">{{ profile_url }}</a></p>'
        ),
        "text_body": (
            "{{ intro_text }}\n\n"
            "Download certificate: {{ certificate_url }}\n"
            "Course page: {{ course_url }}\n\n"
            "Manage preferences: {{ profile_url }}\n"
        ),
        "required_context": [
            {"name": "course_title", "description": "Course title."},
            {"name": "certificate_url", "description": "Certificate URL."},
            {"name": "course_url", "description": "Course page URL."},
            {"name": "profile_url", "description": "Preference settings URL."},
            {"name": "intro_text", "description": "Opening sentence."},
            {"name": "notification_footer", "description": "Preference footer."},
        ],
        "example_context": {
            "email_subject": "Certificate available: ML Zoomcamp",
            "course_title": "ML Zoomcamp",
            "certificate_url": "https://courses.datatalks.club/certificates/example.pdf",
            "course_url": "https://courses.datatalks.club/ml-zoomcamp/",
            "profile_url": "https://courses.datatalks.club/accounts/settings/",
            "intro_text": "Congratulations - your certificate for ML Zoomcamp is available.",
            "notification_footer": "You are receiving this because general course-related emails are enabled.",
        },
        "is_active": True,
    },
    "deadline-reminder": {
        "name": "Deadline Reminder",
        "description": "Remind learners about homework, project, or peer-review deadlines.",
        "subject": "{{ email_subject }}",
        "html_body": (
            "<p>{{ intro_text }}</p>"
            "<p>Deadline: {{ deadline_at }}</p>"
            "<p><a href=\"{{ action_url }}\">{{ action_text|default:'Open course platform' }}</a></p>"
            '<hr><p style="color:#57606a;font-size:13px;line-height:1.5">'
            "{{ notification_footer }} Manage preferences: "
            '<a href="{{ profile_url }}">{{ profile_url }}</a></p>'
        ),
        "text_body": (
            "{{ intro_text }}\n\n"
            "Deadline: {{ deadline_at }}\n"
            "{{ action_text }}\n\n"
            "{{ notification_footer }}\n"
            "Manage preferences: {{ profile_url }}\n"
        ),
        "required_context": [
            {"name": "course_title", "description": "Course title."},
            {"name": "item_title", "description": "Homework, project, or peer-review item title."},
            {"name": "deadline_at", "description": "Deadline timestamp."},
            {"name": "action_url", "description": "URL for the learner action."},
            {"name": "profile_url", "description": "Preference settings URL."},
            {"name": "intro_text", "description": "Opening sentence."},
            {"name": "notification_footer", "description": "Preference footer."},
        ],
        "example_context": {
            "email_subject": "Homework deadline soon: Homework 1",
            "course_title": "ML Zoomcamp",
            "item_type": "homework",
            "item_title": "Homework 1",
            "deadline_at": "2026-06-18T23:00:00+00:00",
            "action_url": "https://courses.datatalks.club/ml-zoomcamp/homework/homework-1",
            "profile_url": "https://courses.datatalks.club/accounts/settings/",
            "intro_text": "Homework 1 in ML Zoomcamp is due within 24 hours.",
            "action_text": "Submit or update homework: https://courses.datatalks.club/ml-zoomcamp/homework/homework-1",
            "notification_footer": "You are receiving this because deadline reminders are enabled.",
        },
        "is_active": True,
    },
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
    parser = argparse.ArgumentParser(description="Create or update CMP transactional templates via the Datamailer API.")
    parser.add_argument("--base-url", default=os.environ.get("DATAMAILER_URL", "http://127.0.0.1:8001"))
    parser.add_argument("--api-key", default=os.environ.get("DATAMAILER_API_KEY", ""))
    parser.add_argument("--template-key", choices=sorted(TEMPLATES), default="")
    args = parser.parse_args()

    if not args.api_key:
        print("DATAMAILER_API_KEY or --api-key is required.", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    template_keys = [args.template_key] if args.template_key else sorted(TEMPLATES)
    results = []
    for template_key in template_keys:
        url = f"{base_url}/api/transactional/templates/{template_key}"
        status, payload = request_json("PUT", url, args.api_key, TEMPLATES[template_key])
        results.append(
            {
                "template_key": template_key,
                "status": status,
                "response": payload,
            }
        )

    print(json.dumps({"templates": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
