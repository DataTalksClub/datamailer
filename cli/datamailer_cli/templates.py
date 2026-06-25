"""Built-in transactional templates the CLI provisions on demand.

Datamailer's send API only delivers through a named, active template. To let
the CLI send ad-hoc ``subject`` + ``body`` messages, we ship two generic
templates and create them lazily the first time they are needed. The body is
passed as template context, so each send reuses the same template.
"""

from __future__ import annotations

# Plain-text body. ``{{ body }}`` is auto-escaped by Django's template engine,
# and ``white-space: pre-wrap`` keeps newlines/indentation from agent output.
TEXT_TEMPLATE_KEY = "cli-message"
TEXT_TEMPLATE = {
    "name": "CLI message",
    "description": "Ad-hoc plain-text message sent with the datamailer CLI.",
    "subject": "{{ subject }}",
    "html_body": (
        '<div style="white-space:pre-wrap;'
        "font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
        'font-size:14px;line-height:1.5">{{ body }}</div>'
    ),
    "text_body": "{{ body }}",
    "required_context": [
        {"name": "subject", "description": "Email subject line."},
        {"name": "body", "description": "Message body."},
    ],
    "example_context": {
        "subject": "Pipeline finished",
        "body": "All 42 jobs completed successfully.",
    },
    "is_active": True,
}

# Raw-HTML body, for agents that already produce an HTML report.
HTML_TEMPLATE_KEY = "cli-message-html"
HTML_TEMPLATE = {
    "name": "CLI message (HTML)",
    "description": "Ad-hoc HTML message sent with the datamailer CLI.",
    "subject": "{{ subject }}",
    "html_body": "{{ body|safe }}",
    "text_body": "{{ body|striptags }}",
    "required_context": [
        {"name": "subject", "description": "Email subject line."},
        {"name": "body", "description": "HTML message body."},
    ],
    "example_context": {
        "subject": "Pipeline finished",
        "body": "<p>All <strong>42</strong> jobs completed successfully.</p>",
    },
    "is_active": True,
}


def template_for(html: bool) -> tuple[str, dict]:
    if html:
        return HTML_TEMPLATE_KEY, HTML_TEMPLATE
    return TEXT_TEMPLATE_KEY, TEXT_TEMPLATE
