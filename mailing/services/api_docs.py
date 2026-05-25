import json
from copy import deepcopy

from django.urls import reverse

from mailing.models import EmailValidationStatus, SubscriptionStatus

DOCS_BASE_URL = "http://127.0.0.1:8002"

DEMO_API_KEYS = [
    {
        "client": "dtc-newsletter",
        "audience": "datatalks-club",
        "name": "Newsletter import/export",
        "env_var": "DATAMAILER_DTC_NEWSLETTER_KEY",
        "raw_key": "dm_dtcnews_demo_newsletter_import_export_key",
        "purpose": "Contact sync, newsletter subscriptions, JSON/CSV import, CSV export.",
    },
    {
        "client": "dtc-courses",
        "audience": "dtc-courses",
        "name": "Course platform transactional",
        "env_var": "DATAMAILER_DTC_COURSES_KEY",
        "raw_key": "dm_dtccourses_demo_transactional_email_key",
        "purpose": "Course registration, password reset, email verification, course contact state.",
    },
    {
        "client": "asl-platform",
        "audience": "ai-shipping-labs",
        "name": "ASL platform transactional",
        "env_var": "DATAMAILER_ASL_PLATFORM_KEY",
        "raw_key": "dm_aslplatform_demo_transactional_email_key",
        "purpose": "AI Shipping Labs transactional and platform contact examples.",
    },
]

API_DOC_PATHS = {
    "mailing:api_contacts": "/api/contacts",
    "mailing:api_contacts_csv": "/api/contacts.csv",
    "mailing:api_contact_imports": "/api/contacts/imports",
    "mailing:api_contact_imports_csv": "/api/contacts/imports/csv",
    "mailing:api_contact_status": "/api/contacts/status",
    "mailing:api_contact_tags": "/api/contacts/{contact_id}/tags",
    "mailing:api_contact_tag": "/api/contacts/{contact_id}/tags/{tag_slug}",
    "mailing:api_contact_verification": "/api/contacts/{contact_id}/verification",
    "mailing:api_contact_validation": "/api/contacts/{contact_id}/validation",
    "mailing:api_contact_suppression": "/api/contacts/{contact_id}/suppression",
    "mailing:api_contact_history": "/api/contacts/{contact_id}/history",
    "mailing:api_subscribe": "/api/subscriptions/subscribe",
    "mailing:api_unsubscribe": "/api/subscriptions/unsubscribe",
    "mailing:api_transactional_send": "/api/transactional/send",
    "mailing:tracking_open": "/t/o/{tracking_token}.gif",
    "mailing:tracking_click": "/t/c/{tracking_token}",
    "mailing:public_unsubscribe": "/unsubscribe/{unsubscribe_token}",
    "mailing:ses_webhook": "/webhooks/ses",
}


def code_json(value):
    return json.dumps(value, indent=2)


def contact_response(*, contact_id=101, email="learner@example.com", audience="dtc-courses", client="dtc-courses", tags=None):
    tags = tags or ["course-ml-zoomcamp"]
    return {
        "contact_id": contact_id,
        "email": email,
        "exists": True,
        "verified": True,
        "verified_at": "2026-05-25T09:30:00Z",
        "email_validation": {
            "status": "externally_validated",
            "reason": "client signup validation",
            "validated_at": "2026-05-25T09:29:00Z",
        },
        "global_unsubscribed": False,
        "hard_bounced": False,
        "complained": False,
        "audience": {
            "slug": audience,
            "subscribed": True,
            "status": "subscribed",
            "verified": True,
            "verified_at": "2026-05-25T09:30:00Z",
            "unsubscribed_at": None,
            "unsubscribe_reason": "",
        },
        "client": {
            "slug": client,
            "subscribed": True,
            "status": "subscribed",
            "verified": True,
            "verified_at": "2026-05-25T09:30:00Z",
            "unsubscribed_at": None,
            "unsubscribe_reason": "",
        },
        "can_send_marketing": True,
        "can_send_transactional": True,
        "tags": tags,
    }


def status_response():
    payload = contact_response()
    payload.pop("tags")
    return payload


def validation_error(fields):
    return {"error": {"code": "validation_error", "fields": fields}}


def workflow_examples():
    return [
        {
            "section": "Setup and Authentication",
            "items": [
                {
                    "id": "setup-auth",
                    "title": "Use a named demo API key",
                    "method": "Header",
                    "path": "Authorization: Bearer",
                    "key": "Course platform transactional",
                    "summary": "Seeded local data creates named keys per client. Staff users can create and revoke additional purpose-specific keys from Clients.",
                    "request": "",
                    "curl": """export DATAMAILER_URL="${DATAMAILER_URL:-http://127.0.0.1:8002}"
export DATAMAILER_API_KEY="dm_dtccourses_demo_transactional_email_key"

curl -sS "$DATAMAILER_URL/api/contacts/status?email=alex.verified@example.com&audience=dtc-courses&client=dtc-courses" \\
  -H "Authorization: Bearer $DATAMAILER_API_KEY" """,
                    "python": """import os
import requests

base_url = os.getenv("DATAMAILER_URL", "http://127.0.0.1:8002")
headers = {"Authorization": f"Bearer {os.environ['DATAMAILER_API_KEY']}"}

response = requests.get(
    f"{base_url}/api/contacts/status",
    headers=headers,
    params={
        "email": "alex.verified@example.com",
        "audience": "dtc-courses",
        "client": "dtc-courses",
    },
    timeout=10,
)
response.raise_for_status()""",
                    "success": code_json(status_response()),
                    "error": code_json({"error": {"code": "invalid_api_key", "message": "Authentication credentials were not accepted."}}),
                },
            ],
        },
        {
            "section": "Contact Workflows",
            "items": [
                {
                    "id": "upsert-contact",
                    "title": "Create or update a contact",
                    "method": "POST",
                    "path": "/api/contacts",
                    "key": "Course platform transactional",
                    "summary": "Upserts the global contact, creates the scoped subscription, adds scoped tags, and records verification/validation state supplied by the trusted client.",
                    "request": code_json(
                        {
                            "email": "learner@example.com",
                            "audience": "dtc-courses",
                            "client": "dtc-courses",
                            "status": "subscribed",
                            "tags": ["course-ml-zoomcamp"],
                            "verified": True,
                            "email_validation": {
                                "status": "externally_validated",
                                "reason": "client signup validation",
                            },
                        }
                    ),
                    "curl": """CONTACT_ID=$(curl -sS -X POST "$DATAMAILER_URL/api/contacts" \\
  -H "Authorization: Bearer $DATAMAILER_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "email": "learner@example.com",
    "audience": "dtc-courses",
    "client": "dtc-courses",
    "status": "subscribed",
    "tags": ["course-ml-zoomcamp"],
    "verified": true,
    "email_validation": {
      "status": "externally_validated",
      "reason": "client signup validation"
    }
  }' | python -c 'import json, sys; print(json.load(sys.stdin)["contact_id"])')
)

echo "Contact ID: $CONTACT_ID" """,
                    "python": "",
                    "success": code_json(contact_response()),
                    "error": code_json(validation_error({"email": "invalid"})),
                },
                {
                    "id": "contact-status",
                    "title": "Check contact status",
                    "method": "GET",
                    "path": "/api/contacts/status",
                    "key": "Course platform transactional",
                    "summary": "Returns subscription, verification, validation, suppression, and sendability state for one email in the authenticated client scope.",
                    "request": "email=learner@example.com&audience=dtc-courses&client=dtc-courses",
                    "curl": """curl -sS "$DATAMAILER_URL/api/contacts/status?email=learner@example.com&audience=dtc-courses&client=dtc-courses" \\
  -H "Authorization: Bearer $DATAMAILER_API_KEY" """,
                    "python": "",
                    "success": code_json(status_response()),
                    "error": code_json(validation_error({"audience": "required"})),
                },
                {
                    "id": "contact-history",
                    "title": "Retrieve contact history",
                    "method": "GET",
                    "path": "/api/contacts/{contact_id}/history",
                    "key": "Course platform transactional",
                    "summary": "Fetches safe scoped campaign, transactional, and event history for troubleshooting support questions.",
                    "request": "audience=dtc-courses&client=dtc-courses&limit=25",
                    "curl": """CONTACT_ID=$(curl -sS "$DATAMAILER_URL/api/contacts/status?email=alex.verified@example.com&audience=dtc-courses&client=dtc-courses" \\
  -H "Authorization: Bearer $DATAMAILER_API_KEY" \\
  | python -c 'import json, sys; print(json.load(sys.stdin)["contact_id"])')
)

curl -sS "$DATAMAILER_URL/api/contacts/$CONTACT_ID/history?audience=dtc-courses&client=dtc-courses&limit=25" \\
  -H "Authorization: Bearer $DATAMAILER_API_KEY" """,
                    "python": "",
                    "success": code_json(
                        {
                            "contact_id": 101,
                            "email": "learner@example.com",
                            "audience": "dtc-courses",
                            "client": "dtc-courses",
                            "campaign_recipients": [],
                            "transactional_messages": [
                                {
                                    "type": "transactional_message",
                                    "id": 901,
                                    "template_key": "registration-welcome",
                                    "status": "queued",
                                    "subject": "Welcome to ML Zoomcamp",
                                }
                            ],
                            "events": [{"type": "email_event", "id": 1201, "event_type": "queued", "metadata": {}}],
                            "next_cursor": None,
                        }
                    ),
                    "error": code_json(validation_error({"contact_id": "not_found"})),
                },
            ],
        },
        {
            "section": "Subscription and Tag Workflows",
            "items": [
                {
                    "id": "subscribe-contact",
                    "title": "Subscribe a contact",
                    "method": "POST",
                    "path": "/api/subscriptions/subscribe",
                    "key": "Newsletter import/export",
                    "summary": "Creates the contact if needed and marks the client-scoped subscription subscribed.",
                    "request": code_json(
                        {
                            "email": "subscriber@example.com",
                            "audience": "datatalks-club",
                            "client": "dtc-newsletter",
                            "tags": ["newsletter"],
                        }
                    ),
                    "curl": """NEWSLETTER_CONTACT_ID=$(curl -sS -X POST "$DATAMAILER_URL/api/subscriptions/subscribe" \\
  -H "Authorization: Bearer $DATAMAILER_DTC_NEWSLETTER_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"email":"subscriber@example.com","audience":"datatalks-club","client":"dtc-newsletter","tags":["newsletter"]}' \\
  | python -c 'import json, sys; print(json.load(sys.stdin)["contact_id"])')
)

echo "Newsletter contact ID: $NEWSLETTER_CONTACT_ID" """,
                    "python": "",
                    "success": code_json(
                        contact_response(
                            contact_id=202,
                            email="subscriber@example.com",
                            audience="datatalks-club",
                            client="dtc-newsletter",
                            tags=["newsletter"],
                        )
                    ),
                    "error": code_json(validation_error({"client": "forbidden"})),
                },
                {
                    "id": "unsubscribe-contact",
                    "title": "Unsubscribe a contact",
                    "method": "POST",
                    "path": "/api/subscriptions/unsubscribe",
                    "key": "Newsletter import/export",
                    "summary": "Applies a client, audience, or global unsubscribe while preserving an audit-friendly reason.",
                    "request": code_json(
                        {
                            "email": "subscriber@example.com",
                            "audience": "datatalks-club",
                            "client": "dtc-newsletter",
                            "scope": "client",
                            "reason": "user_requested",
                        }
                    ),
                    "curl": """curl -sS -X POST "$DATAMAILER_URL/api/subscriptions/unsubscribe" \\
  -H "Authorization: Bearer $DATAMAILER_DTC_NEWSLETTER_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"email":"subscriber@example.com","audience":"datatalks-club","client":"dtc-newsletter","scope":"client","reason":"user_requested"}'""",
                    "python": "",
                    "success": code_json(
                        {
                            **status_response(),
                            "email": "subscriber@example.com",
                            "scope": "client",
                            "client": {
                                "slug": "dtc-newsletter",
                                "subscribed": False,
                                "status": "unsubscribed",
                                "verified": False,
                                "verified_at": None,
                                "unsubscribed_at": "2026-05-25T10:00:00Z",
                                "unsubscribe_reason": "user_requested",
                            },
                        }
                    ),
                    "error": code_json(validation_error({"scope": "invalid"})),
                },
                {
                    "id": "replace-tags",
                    "title": "Replace contact tags",
                    "method": "PUT",
                    "path": "/api/contacts/{contact_id}/tags",
                    "key": "Newsletter import/export",
                    "summary": "Replaces the contact's tag set for the target audience.",
                    "request": code_json({"audience": "datatalks-club", "client": "dtc-newsletter", "tags": ["newsletter", "events"]}),
                    "curl": """NEWSLETTER_CONTACT_ID=$(curl -sS "$DATAMAILER_URL/api/contacts/status?email=alex.verified@example.com&audience=datatalks-club&client=dtc-newsletter" \\
  -H "Authorization: Bearer $DATAMAILER_DTC_NEWSLETTER_KEY" \\
  | python -c 'import json, sys; print(json.load(sys.stdin)["contact_id"])')
)

curl -sS -X PUT "$DATAMAILER_URL/api/contacts/$NEWSLETTER_CONTACT_ID/tags" \\
  -H "Authorization: Bearer $DATAMAILER_DTC_NEWSLETTER_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"audience":"datatalks-club","client":"dtc-newsletter","tags":["newsletter","events"]}'""",
                    "python": "",
                    "success": code_json(
                        contact_response(
                            contact_id=202,
                            email="subscriber@example.com",
                            audience="datatalks-club",
                            client="dtc-newsletter",
                            tags=["events", "newsletter"],
                        )
                    ),
                    "error": code_json(validation_error({"tags": "must_be_list"})),
                },
                {
                    "id": "add-remove-tag",
                    "title": "Add and remove one tag",
                    "method": "POST/DELETE",
                    "path": "/api/contacts/{contact_id}/tags/{tag_slug}",
                    "key": "Newsletter import/export",
                    "summary": "Use single-tag mutations when a client workflow toggles one audience tag at a time.",
                    "request": code_json({"audience": "datatalks-club", "client": "dtc-newsletter"}),
                    "curl": """NEWSLETTER_CONTACT_ID=$(curl -sS "$DATAMAILER_URL/api/contacts/status?email=alex.verified@example.com&audience=datatalks-club&client=dtc-newsletter" \\
  -H "Authorization: Bearer $DATAMAILER_DTC_NEWSLETTER_KEY" \\
  | python -c 'import json, sys; print(json.load(sys.stdin)["contact_id"])')
)

curl -sS -X POST "$DATAMAILER_URL/api/contacts/$NEWSLETTER_CONTACT_ID/tags/events" \\
  -H "Authorization: Bearer $DATAMAILER_DTC_NEWSLETTER_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"audience":"datatalks-club","client":"dtc-newsletter"}'

curl -sS -X DELETE "$DATAMAILER_URL/api/contacts/$NEWSLETTER_CONTACT_ID/tags/events?audience=datatalks-club&client=dtc-newsletter" \\
  -H "Authorization: Bearer $DATAMAILER_DTC_NEWSLETTER_KEY" """,
                    "python": "",
                    "success": code_json(
                        contact_response(
                            contact_id=202,
                            email="subscriber@example.com",
                            audience="datatalks-club",
                            client="dtc-newsletter",
                            tags=["newsletter"],
                        )
                    ),
                    "error": code_json(validation_error({"tag": "invalid"})),
                },
            ],
        },
        {
            "section": "Verification, Validation, and Suppression",
            "items": [
                {
                    "id": "mark-verified",
                    "title": "Mark email verified",
                    "method": "PATCH",
                    "path": "/api/contacts/{contact_id}/verification",
                    "key": "Course platform transactional",
                    "summary": "After a user verifies in the client app, mark the Datamailer contact verified for marketing eligibility.",
                    "request": code_json({"audience": "dtc-courses", "client": "dtc-courses", "verified": True}),
                    "curl": """CONTACT_ID=$(curl -sS "$DATAMAILER_URL/api/contacts/status?email=alex.verified@example.com&audience=dtc-courses&client=dtc-courses" \\
  -H "Authorization: Bearer $DATAMAILER_API_KEY" \\
  | python -c 'import json, sys; print(json.load(sys.stdin)["contact_id"])')
)

curl -sS -X PATCH "$DATAMAILER_URL/api/contacts/$CONTACT_ID/verification" \\
  -H "Authorization: Bearer $DATAMAILER_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"audience":"dtc-courses","client":"dtc-courses","verified":true}'""",
                    "python": "",
                    "success": code_json(contact_response()),
                    "error": code_json(validation_error({"verified": "required"})),
                },
                {
                    "id": "validation-state",
                    "title": "Set validation state",
                    "method": "PATCH",
                    "path": "/api/contacts/{contact_id}/validation",
                    "key": "Course platform transactional",
                    "summary": "Stores external validation or manual hygiene decisions used by sendability checks.",
                    "request": code_json(
                        {
                            "audience": "dtc-courses",
                            "client": "dtc-courses",
                            "status": "externally_validated",
                            "reason": "validated at signup",
                        }
                    ),
                    "curl": """CONTACT_ID=$(curl -sS "$DATAMAILER_URL/api/contacts/status?email=alex.verified@example.com&audience=dtc-courses&client=dtc-courses" \\
  -H "Authorization: Bearer $DATAMAILER_API_KEY" \\
  | python -c 'import json, sys; print(json.load(sys.stdin)["contact_id"])')
)

curl -sS -X PATCH "$DATAMAILER_URL/api/contacts/$CONTACT_ID/validation" \\
  -H "Authorization: Bearer $DATAMAILER_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"audience":"dtc-courses","client":"dtc-courses","status":"externally_validated","reason":"validated at signup"}'""",
                    "python": "",
                    "success": code_json(contact_response()),
                    "error": code_json(validation_error({"email_validation.status": "invalid"})),
                },
                {
                    "id": "suppression-state",
                    "title": "Set suppression state",
                    "method": "PATCH",
                    "path": "/api/contacts/{contact_id}/suppression",
                    "key": "Newsletter import/export",
                    "summary": "Records hard bounce, complaint, or global unsubscribe state when an external system owns the event source.",
                    "request": code_json(
                        {
                            "audience": "datatalks-club",
                            "client": "dtc-newsletter",
                            "hard_bounced": True,
                            "reason": "provider_hard_bounce",
                        }
                    ),
                    "curl": """NEWSLETTER_CONTACT_ID=$(curl -sS "$DATAMAILER_URL/api/contacts/status?email=alex.verified@example.com&audience=datatalks-club&client=dtc-newsletter" \\
  -H "Authorization: Bearer $DATAMAILER_DTC_NEWSLETTER_KEY" \\
  | python -c 'import json, sys; print(json.load(sys.stdin)["contact_id"])')
)

curl -sS -X PATCH "$DATAMAILER_URL/api/contacts/$NEWSLETTER_CONTACT_ID/suppression" \\
  -H "Authorization: Bearer $DATAMAILER_DTC_NEWSLETTER_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"audience":"datatalks-club","client":"dtc-newsletter","hard_bounced":true,"reason":"provider_hard_bounce"}'""",
                    "python": "",
                    "success": code_json(
                        {
                            **contact_response(
                                contact_id=202,
                                email="subscriber@example.com",
                                audience="datatalks-club",
                                client="dtc-newsletter",
                                tags=["newsletter"],
                            ),
                            "hard_bounced": True,
                            "can_send_marketing": False,
                            "can_send_transactional": False,
                        }
                    ),
                    "error": code_json(validation_error({"suppression.hard_bounced": "must_be_boolean"})),
                },
            ],
        },
        {
            "section": "Imports, Exports, and Transactional Send",
            "items": [
                {
                    "id": "transactional-send",
                    "title": "Send transactional email",
                    "method": "POST",
                    "path": "/api/transactional/send",
                    "key": "Course platform transactional",
                    "summary": "Requires the Datamailer server to be started with SQS_TRANSACTIONAL_EMAIL_QUEUE_URL configured, for example through LocalStack. With the default empty queue URL, this endpoint is documented but is not runnable because queueing provider work will fail. When configured, it validates template context, creates a transactional message, and enqueues provider work.",
                    "request": code_json(
                        {
                            "email": "learner@example.com",
                            "template_key": "registration-welcome",
                            "idempotency_key": "registration-user-123",
                            "context": {"name": "Learner", "course_name": "ML Zoomcamp"},
                            "metadata": {"source": "registration"},
                        }
                    ),
                    "curl": """curl -sS -X POST "$DATAMAILER_URL/api/transactional/send" \\
  -H "Authorization: Bearer $DATAMAILER_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "email": "learner@example.com",
    "template_key": "registration-welcome",
    "idempotency_key": "registration-user-123",
    "context": {"name": "Learner", "course_name": "ML Zoomcamp"},
    "metadata": {"source": "registration"}
  }'""",
                    "python": """requests.post(
    f"{base_url}/api/transactional/send",
    headers=headers,
    json={
        "email": "learner@example.com",
        "template_key": "registration-welcome",
        "idempotency_key": "registration-user-123",
        "context": {"name": "Learner", "course_name": "ML Zoomcamp"},
        "metadata": {"source": "registration"},
    },
    timeout=10,
)

requests.post(
    f"{base_url}/api/transactional/send",
    headers=headers,
    json={
        "email": "learner@example.com",
        "template_key": "password-reset",
        "idempotency_key": "password-reset-user-123-request-456",
        "context": {"reset_url": "https://client.example/reset/placeholder"},
        "metadata": {"source": "password-reset"},
    },
    timeout=10,
)

requests.post(
    f"{base_url}/api/transactional/send",
    headers=headers,
    json={
        "email": "learner@example.com",
        "template_key": "email-verification",
        "idempotency_key": "verify-user-123-email-1",
        "context": {
            "name": "Learner",
            "verification_url": "https://client.example/verify/placeholder",
        },
        "metadata": {"source": "email-verification"},
    },
    timeout=10,
)""",
                    "success": code_json(
                        {
                            "message": {
                                "id": 901,
                                "email": "learner@example.com",
                                "template_key": "registration-welcome",
                                "status": "queued",
                            },
                            "idempotent_replay": False,
                            "enqueued": True,
                        }
                    ),
                    "error": code_json(validation_error({"context.course_name": "required"})),
                },
                {
                    "id": "json-import",
                    "title": "Import contacts with JSON",
                    "method": "POST",
                    "path": "/api/contacts/imports",
                    "key": "Newsletter import/export",
                    "summary": "Bulk upserts contacts. Invalid rows are reported in partial errors while valid rows continue.",
                    "request": code_json(
                        {
                            "audience": "datatalks-club",
                            "client": "dtc-newsletter",
                            "dry_run": False,
                            "idempotency_key": "newsletter-import-2026-05-25",
                            "contacts": [
                                {
                                    "email": "subscriber@example.com",
                                    "status": "subscribed",
                                    "tags": ["newsletter"],
                                    "email_validation": {"status": "externally_validated"},
                                }
                            ],
                        }
                    ),
                    "curl": """curl -sS -X POST "$DATAMAILER_URL/api/contacts/imports" \\
  -H "Authorization: Bearer $DATAMAILER_DTC_NEWSLETTER_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "audience": "datatalks-club",
    "client": "dtc-newsletter",
    "dry_run": false,
    "idempotency_key": "newsletter-import-2026-05-25",
    "contacts": [{"email":"subscriber@example.com","status":"subscribed","tags":["newsletter"]}]
  }'""",
                    "python": "",
                    "success": code_json(
                        {
                            "dry_run": False,
                            "idempotency_key": "newsletter-import-2026-05-25",
                            "counts": {"total": 1, "created": 1, "updated": 0, "unchanged": 0, "skipped": 0, "invalid": 0},
                            "results": [{"index": 0, "item": 1, "action": "created", "contact": {"contact_id": 202}}],
                            "errors": [],
                        }
                    ),
                    "error": code_json(validation_error({"contacts": "must_be_list"})),
                },
                {
                    "id": "csv-import",
                    "title": "Import contacts with CSV",
                    "method": "POST",
                    "path": "/api/contacts/imports/csv",
                    "key": "Newsletter import/export",
                    "summary": "Uploads CSV using the same column semantics as export. JSON with a csv string also works for local scripts.",
                    "request": "email,tags,subscription_status,verified\nsubscriber@example.com,newsletter,subscribed,true",
                    "curl": """curl -sS -X POST "$DATAMAILER_URL/api/contacts/imports/csv" \\
  -H "Authorization: Bearer $DATAMAILER_DTC_NEWSLETTER_KEY" \\
  -F audience=datatalks-club \\
  -F client=dtc-newsletter \\
  -F dry_run=false \\
  -F file=@contacts.csv""",
                    "python": "",
                    "success": code_json(
                        {
                            "dry_run": False,
                            "idempotency_key": "",
                            "counts": {"total": 1, "created": 0, "updated": 1, "unchanged": 0, "skipped": 0, "invalid": 0},
                            "results": [{"index": 0, "item": 1, "action": "updated", "contact": {"contact_id": 202}}],
                            "errors": [],
                        }
                    ),
                    "error": code_json(validation_error({"csv": "required"})),
                },
                {
                    "id": "csv-export",
                    "title": "Export contacts as CSV",
                    "method": "GET",
                    "path": "/api/contacts.csv",
                    "key": "Newsletter import/export",
                    "summary": "Exports safe recreatable contact, subscription, tag, verification, validation, suppression, and update timestamp columns.",
                    "request": "audience=datatalks-club&client=dtc-newsletter&tags=newsletter",
                    "curl": """curl -sS "$DATAMAILER_URL/api/contacts.csv?audience=datatalks-club&client=dtc-newsletter&tags=newsletter" \\
  -H "Authorization: Bearer $DATAMAILER_DTC_NEWSLETTER_KEY" \\
  -o contacts.csv""",
                    "python": "",
                    "success": "email,audience,client,tags,subscription_status,verified,verified_at,email_validation_status\nsubscriber@example.com,datatalks-club,dtc-newsletter,newsletter,subscribed,true,2026-05-25T09:30:00Z,externally_validated",
                    "error": code_json(validation_error({"subscription_status": "invalid"})),
                },
            ],
        },
    ]


def endpoint_groups():
    return [
        {
            "name": "Contacts",
            "endpoints": [
                ("POST", "/api/contacts", "Upsert one contact in an audience/client scope."),
                ("GET", "/api/contacts/status", "Look up contact status for one scoped email."),
                ("GET", "/api/contacts", "List/export contacts as paginated JSON."),
                ("GET", "/api/contacts.csv", "Export contacts as CSV."),
            ],
        },
        {
            "name": "Subscriptions and Tags",
            "endpoints": [
                ("POST", "/api/subscriptions/subscribe", "Subscribe one scoped contact."),
                ("POST", "/api/subscriptions/unsubscribe", "Unsubscribe one scoped contact."),
                ("PUT", "/api/contacts/{contact_id}/tags", "Replace one contact's scoped tags."),
                ("POST", "/api/contacts/{contact_id}/tags/{tag_slug}", "Add one scoped tag."),
                ("DELETE", "/api/contacts/{contact_id}/tags/{tag_slug}", "Remove one scoped tag."),
            ],
        },
        {
            "name": "State and History",
            "endpoints": [
                ("PATCH", "/api/contacts/{contact_id}/verification", "Set verification state."),
                ("PATCH", "/api/contacts/{contact_id}/validation", "Set email validation state."),
                ("PATCH", "/api/contacts/{contact_id}/suppression", "Set suppression state."),
                ("GET", "/api/contacts/{contact_id}/history", "Return safe scoped send and event history."),
            ],
        },
        {
            "name": "Imports and Transactional",
            "endpoints": [
                ("POST", "/api/contacts/imports", "Bulk JSON import/upsert."),
                ("POST", "/api/contacts/imports/csv", "CSV upload/import."),
                ("POST", "/api/transactional/send", "Queue one transactional email."),
            ],
        },
        {
            "name": "Public and Provider",
            "endpoints": [
                ("GET", "/t/o/{tracking_token}.gif", "Open tracking pixel."),
                ("GET", "/t/c/{tracking_token}", "Click tracking redirect."),
                ("GET/POST", "/unsubscribe/{unsubscribe_token}", "Public unsubscribe form."),
                ("POST", "/webhooks/ses", "SES/SNS provider webhook ingress."),
            ],
        },
    ]


def build_openapi_spec(request=None):
    spec = deepcopy(OPENAPI_SPEC)
    if request is not None:
        spec["servers"] = [{"url": request.build_absolute_uri("/").rstrip("/")}]
    return spec


def route_path_map():
    return {
        API_DOC_PATHS["mailing:api_contacts"]: reverse("mailing:api_contacts"),
        API_DOC_PATHS["mailing:api_contacts_csv"]: reverse("mailing:api_contacts_csv"),
        API_DOC_PATHS["mailing:api_contact_imports"]: reverse("mailing:api_contact_imports"),
        API_DOC_PATHS["mailing:api_contact_imports_csv"]: reverse("mailing:api_contact_imports_csv"),
        API_DOC_PATHS["mailing:api_contact_status"]: reverse("mailing:api_contact_status"),
        API_DOC_PATHS["mailing:api_contact_tags"]: reverse("mailing:api_contact_tags", args=[123]),
        API_DOC_PATHS["mailing:api_contact_tag"]: reverse("mailing:api_contact_tag", args=[123, "newsletter"]),
        API_DOC_PATHS["mailing:api_contact_verification"]: reverse("mailing:api_contact_verification", args=[123]),
        API_DOC_PATHS["mailing:api_contact_validation"]: reverse("mailing:api_contact_validation", args=[123]),
        API_DOC_PATHS["mailing:api_contact_suppression"]: reverse("mailing:api_contact_suppression", args=[123]),
        API_DOC_PATHS["mailing:api_contact_history"]: reverse("mailing:api_contact_history", args=[123]),
        API_DOC_PATHS["mailing:api_subscribe"]: reverse("mailing:api_subscribe"),
        API_DOC_PATHS["mailing:api_unsubscribe"]: reverse("mailing:api_unsubscribe"),
        API_DOC_PATHS["mailing:api_transactional_send"]: reverse("mailing:api_transactional_send"),
        API_DOC_PATHS["mailing:tracking_open"]: reverse("mailing:tracking_open", args=["tracking"]),
        API_DOC_PATHS["mailing:tracking_click"]: reverse("mailing:tracking_click", args=["tracking"]),
        API_DOC_PATHS["mailing:public_unsubscribe"]: reverse("mailing:public_unsubscribe", args=["unsubscribe"]),
        API_DOC_PATHS["mailing:ses_webhook"]: reverse("mailing:ses_webhook"),
    }


def json_response(description="OK", schema_ref=None):
    content = {"application/json": {"schema": {"$ref": schema_ref}}} if schema_ref else {}
    return {"description": description, "content": content}


def csv_response(description="CSV file"):
    return {
        "description": description,
        "content": {"text/csv": {"schema": {"type": "string"}}},
    }


def json_body(schema_ref, *, required=True):
    return {
        "required": required,
        "content": {"application/json": {"schema": {"$ref": schema_ref}}},
    }


def bearer_responses(success, *, accepted=False):
    responses = {"200": success, "400": {"$ref": "#/components/responses/ValidationError"}}
    if accepted:
        responses = {"202": success, "400": {"$ref": "#/components/responses/ValidationError"}}
    responses["401"] = {"$ref": "#/components/responses/Unauthorized"}
    responses["403"] = {"$ref": "#/components/responses/Forbidden"}
    responses["405"] = {"$ref": "#/components/responses/MethodNotAllowed"}
    return responses


CONTACT_ID_PARAM = {"name": "contact_id", "in": "path", "required": True, "schema": {"type": "integer"}}
TAG_SLUG_PARAM = {"name": "tag_slug", "in": "path", "required": True, "schema": {"type": "string"}}
TRACKING_PARAM = {"name": "tracking_token", "in": "path", "required": True, "schema": {"type": "string"}}
UNSUBSCRIBE_PARAM = {"name": "unsubscribe_token", "in": "path", "required": True, "schema": {"type": "string"}}

SCOPE_QUERY_PARAMS = [
    {"name": "email", "in": "query", "required": True, "schema": {"type": "string", "format": "email"}},
    {"name": "audience", "in": "query", "required": True, "schema": {"type": "string"}},
    {"name": "client", "in": "query", "required": True, "schema": {"type": "string"}},
]

EXPORT_QUERY_PARAMS = [
    {"name": "audience", "in": "query", "required": True, "schema": {"type": "string"}},
    {"name": "client", "in": "query", "required": True, "schema": {"type": "string"}},
    {"name": "tags", "in": "query", "schema": {"type": "string", "description": "Comma-separated tag slugs."}},
    {"name": "subscription_status", "in": "query", "schema": {"$ref": "#/components/schemas/SubscriptionStatus"}},
    {"name": "verified", "in": "query", "schema": {"type": "boolean"}},
    {"name": "email_validation_status", "in": "query", "schema": {"$ref": "#/components/schemas/EmailValidationStatus"}},
    {
        "name": "suppression",
        "in": "query",
        "schema": {
            "type": "string",
            "enum": ["none", "any", "global_unsubscribed", "hard_bounced", "complained"],
        },
    },
    {"name": "updated_since", "in": "query", "schema": {"type": "string", "format": "date-time"}},
    {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1}},
    {"name": "cursor", "in": "query", "schema": {"type": "integer", "minimum": 1}},
    {"name": "offset", "in": "query", "schema": {"type": "integer", "minimum": 1}},
]

OPENAPI_SPEC = {
    "openapi": "3.1.0",
    "info": {
        "title": "Datamailer Native API",
        "version": "1.0.0",
        "description": "Local staff reference for implemented Datamailer endpoints. Client API routes use Bearer authentication with named Datamailer client API keys. Transactional templates are planned for catalog management and may be provisioned externally for now.",
    },
    "servers": [{"url": "/"}],
    "tags": [
        {"name": "Contacts"},
        {"name": "Subscriptions"},
        {"name": "Tags"},
        {"name": "State"},
        {"name": "Imports"},
        {"name": "Transactional"},
        {"name": "Public"},
        {"name": "Provider"},
    ],
    "paths": {
        "/api/contacts": {
            "get": {
                "tags": ["Contacts"],
                "summary": "List contacts",
                "security": [{"BearerAuth": []}],
                "parameters": EXPORT_QUERY_PARAMS,
                "responses": bearer_responses(json_response("Contacts list", "#/components/schemas/ContactListResponse")),
            },
            "post": {
                "tags": ["Contacts"],
                "summary": "Upsert contact",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/ContactUpsertRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            },
        },
        "/api/contacts.csv": {
            "get": {
                "tags": ["Contacts"],
                "summary": "Export contacts CSV",
                "description": "Exports safe recreatable contact, subscription, tag, verification, validation, suppression, unsubscribe, and update timestamp columns. Secret hashes and delivery link tokens are never exported.",
                "security": [{"BearerAuth": []}],
                "parameters": EXPORT_QUERY_PARAMS,
                "responses": bearer_responses(csv_response()),
            }
        },
        "/api/contacts/imports": {
            "post": {
                "tags": ["Imports"],
                "summary": "Bulk import contacts",
                "description": "Imports are idempotent by normalized email plus audience/client scope. Invalid items are returned in partial errors while other items continue.",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/BulkImportRequest"),
                "responses": bearer_responses(json_response("Import result", "#/components/schemas/ImportResult")),
            }
        },
        "/api/contacts/imports/csv": {
            "post": {
                "tags": ["Imports"],
                "summary": "Import contacts CSV",
                "description": "Accepts CSV text in JSON or an uploaded file using the export column semantics. Invalid rows are reported without aborting valid rows.",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {"schema": {"$ref": "#/components/schemas/CsvImportUpload"}},
                        "application/json": {"schema": {"$ref": "#/components/schemas/CsvImportJson"}},
                    },
                },
                "responses": bearer_responses(json_response("Import result", "#/components/schemas/ImportResult")),
            }
        },
        "/api/contacts/status": {
            "get": {
                "tags": ["Contacts"],
                "summary": "Get contact status",
                "security": [{"BearerAuth": []}],
                "parameters": SCOPE_QUERY_PARAMS,
                "responses": bearer_responses(json_response("Contact status", "#/components/schemas/ContactStatus")),
            }
        },
        "/api/contacts/{contact_id}/tags": {
            "put": {
                "tags": ["Tags"],
                "summary": "Replace contact tags",
                "security": [{"BearerAuth": []}],
                "parameters": [CONTACT_ID_PARAM],
                "requestBody": json_body("#/components/schemas/TagReplaceRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            }
        },
        "/api/contacts/{contact_id}/tags/{tag_slug}": {
            "post": {
                "tags": ["Tags"],
                "summary": "Add contact tag",
                "security": [{"BearerAuth": []}],
                "parameters": [CONTACT_ID_PARAM, TAG_SLUG_PARAM],
                "requestBody": json_body("#/components/schemas/ScopedMutationRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            },
            "delete": {
                "tags": ["Tags"],
                "summary": "Remove contact tag",
                "security": [{"BearerAuth": []}],
                "parameters": [CONTACT_ID_PARAM, TAG_SLUG_PARAM],
                "requestBody": json_body("#/components/schemas/ScopedMutationRequest", required=False),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            },
        },
        "/api/contacts/{contact_id}/verification": {
            "patch": {
                "tags": ["State"],
                "summary": "Update verification",
                "security": [{"BearerAuth": []}],
                "parameters": [CONTACT_ID_PARAM],
                "requestBody": json_body("#/components/schemas/VerificationRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            }
        },
        "/api/contacts/{contact_id}/validation": {
            "patch": {
                "tags": ["State"],
                "summary": "Update email validation",
                "security": [{"BearerAuth": []}],
                "parameters": [CONTACT_ID_PARAM],
                "requestBody": json_body("#/components/schemas/ValidationRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            }
        },
        "/api/contacts/{contact_id}/suppression": {
            "patch": {
                "tags": ["State"],
                "summary": "Update suppression",
                "security": [{"BearerAuth": []}],
                "parameters": [CONTACT_ID_PARAM],
                "requestBody": json_body("#/components/schemas/SuppressionRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            }
        },
        "/api/contacts/{contact_id}/history": {
            "get": {
                "tags": ["Contacts"],
                "summary": "Get contact history",
                "security": [{"BearerAuth": []}],
                "parameters": [
                    CONTACT_ID_PARAM,
                    {"name": "audience", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "client", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100}},
                    {"name": "cursor", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                ],
                "responses": bearer_responses(json_response("Contact history", "#/components/schemas/ContactHistory")),
            }
        },
        "/api/subscriptions/subscribe": {
            "post": {
                "tags": ["Subscriptions"],
                "summary": "Subscribe contact",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/SubscribeRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            }
        },
        "/api/subscriptions/unsubscribe": {
            "post": {
                "tags": ["Subscriptions"],
                "summary": "Unsubscribe contact",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/UnsubscribeRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/ContactStatus")),
            }
        },
        "/api/transactional/send": {
            "post": {
                "tags": ["Transactional"],
                "summary": "Send transactional email",
                "description": "Queues one transactional email for an active client-scoped template. Required context is validated from template catalog metadata before any contact, message, event, or queue mutation.",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/TransactionalSendRequest"),
                "responses": bearer_responses(
                    json_response("Transactional message accepted", "#/components/schemas/TransactionalSendResponse"),
                    accepted=True,
                )
                | {"409": json_response("Contact suppressed", "#/components/schemas/TransactionalSendResponse")},
            }
        },
        "/t/o/{tracking_token}.gif": {
            "get": {
                "tags": ["Public"],
                "summary": "Open tracking pixel",
                "parameters": [TRACKING_PARAM],
                "responses": {"200": {"description": "Transparent GIF"}, "404": {"description": "Transparent GIF"}},
            }
        },
        "/t/c/{tracking_token}": {
            "get": {
                "tags": ["Public"],
                "summary": "Click tracking redirect",
                "parameters": [
                    TRACKING_PARAM,
                    {"name": "u", "in": "query", "required": True, "schema": {"type": "string", "format": "uri"}},
                ],
                "responses": {
                    "302": {"description": "Redirects to destination URL"},
                    "400": {"$ref": "#/components/responses/ValidationError"},
                },
            }
        },
        "/unsubscribe/{unsubscribe_token}": {
            "get": {
                "tags": ["Public"],
                "summary": "Render unsubscribe form",
                "parameters": [UNSUBSCRIBE_PARAM],
                "responses": {"200": {"description": "HTML form"}, "404": {"description": "HTML not found"}},
            },
            "post": {
                "tags": ["Public"],
                "summary": "Apply unsubscribe",
                "parameters": [UNSUBSCRIBE_PARAM],
                "responses": {"200": {"description": "HTML confirmation"}, "400": {"description": "HTML validation error"}},
            },
        },
        "/webhooks/ses": {
            "post": {
                "tags": ["Provider"],
                "summary": "SES/SNS webhook ingress",
                "description": "Provider ingress for Amazon SES notification messages. Requests are validated as SNS messages by the webhook service.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {
                    "200": json_response("Webhook accepted"),
                    "400": {"$ref": "#/components/responses/ValidationError"},
                    "403": json_response("SNS signature rejected"),
                },
            }
        },
    },
    "components": {
        "securitySchemes": {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "Datamailer client API key",
            }
        },
        "responses": {
            "Unauthorized": json_response("Authentication failed", "#/components/schemas/ErrorResponse"),
            "Forbidden": json_response("Scope forbidden", "#/components/schemas/ErrorResponse"),
            "ValidationError": json_response("Validation error", "#/components/schemas/ErrorResponse"),
            "MethodNotAllowed": json_response("Method not allowed", "#/components/schemas/ErrorResponse"),
        },
        "schemas": {
            "ErrorResponse": {
                "type": "object",
                "required": ["error"],
                "properties": {
                    "error": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "message": {"type": "string"},
                            "fields": {"type": "object"},
                            "allowed_methods": {"type": "array", "items": {"type": "string"}},
                        },
                    }
                },
            },
            "SubscriptionStatus": {"type": "string", "enum": [choice.value for choice in SubscriptionStatus]},
            "EmailValidationStatus": {"type": "string", "enum": [choice.value for choice in EmailValidationStatus]},
            "ScopedMutationRequest": {
                "type": "object",
                "required": ["audience", "client"],
                "properties": {"audience": {"type": "string"}, "client": {"type": "string"}},
            },
            "ContactUpsertRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["email"],
                        "properties": {
                            "email": {"type": "string", "format": "email"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                            "status": {"$ref": "#/components/schemas/SubscriptionStatus"},
                            "verified": {"type": "boolean"},
                            "email_validation": {"$ref": "#/components/schemas/EmailValidationInput"},
                            "suppression": {"$ref": "#/components/schemas/SuppressionFlags"},
                        },
                    },
                ]
            },
            "SubscribeRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ContactUpsertRequest"},
                    {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}},
                ]
            },
            "UnsubscribeRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["email", "scope"],
                        "properties": {
                            "email": {"type": "string", "format": "email"},
                            "scope": {"type": "string", "enum": ["client", "audience", "global"]},
                            "reason": {"type": "string"},
                        },
                    },
                ]
            },
            "TagReplaceRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["tags"],
                        "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
                    },
                ]
            },
            "VerificationRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["verified"],
                        "properties": {
                            "verified": {"type": "boolean"},
                            "verified_at": {"type": "string", "format": "date-time"},
                        },
                    },
                ]
            },
            "ValidationRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["status"],
                        "properties": {
                            "status": {"$ref": "#/components/schemas/EmailValidationStatus"},
                            "reason": {"type": "string"},
                            "validated_at": {"type": "string", "format": "date-time"},
                        },
                    },
                ]
            },
            "EmailValidationInput": {
                "type": "object",
                "properties": {
                    "status": {"$ref": "#/components/schemas/EmailValidationStatus"},
                    "reason": {"type": "string"},
                    "validated_at": {"type": "string", "format": "date-time"},
                },
            },
            "SuppressionFlags": {
                "type": "object",
                "properties": {
                    "global_unsubscribed": {"type": "boolean"},
                    "hard_bounced": {"type": "boolean"},
                    "complained": {"type": "boolean"},
                },
            },
            "SuppressionRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "properties": {
                            "global_unsubscribed": {"type": "boolean"},
                            "hard_bounced": {"type": "boolean"},
                            "complained": {"type": "boolean"},
                            "reason": {"type": "string"},
                        },
                    },
                ]
            },
            "BulkImportRequest": {
                "type": "object",
                "required": ["contacts"],
                "properties": {
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "dry_run": {"type": "boolean", "description": "Validate and report would-create/update actions."},
                    "idempotency_key": {"type": "string", "description": "Echoed for client-side run correlation."},
                    "contacts": {"type": "array", "items": {"$ref": "#/components/schemas/ContactUpsertRequest"}},
                },
            },
            "CsvImportUpload": {
                "type": "object",
                "required": ["file"],
                "properties": {
                    "file": {"type": "string", "format": "binary"},
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                    "idempotency_key": {"type": "string"},
                },
            },
            "CsvImportJson": {
                "type": "object",
                "required": ["csv"],
                "properties": {
                    "csv": {"type": "string"},
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                    "idempotency_key": {"type": "string"},
                },
            },
            "ContactStatus": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": ["integer", "null"]},
                    "email": {"type": "string"},
                    "exists": {"type": "boolean"},
                    "verified": {"type": "boolean"},
                    "verified_at": {"type": ["string", "null"], "format": "date-time"},
                    "email_validation": {"$ref": "#/components/schemas/EmailValidationState"},
                    "global_unsubscribed": {"type": "boolean"},
                    "hard_bounced": {"type": "boolean"},
                    "complained": {"type": "boolean"},
                    "can_send_marketing": {"type": "boolean"},
                    "can_send_transactional": {"type": "boolean"},
                    "audience": {"$ref": "#/components/schemas/SubscriptionState"},
                    "client": {"$ref": "#/components/schemas/SubscriptionState"},
                },
            },
            "Contact": {
                "allOf": [
                    {"$ref": "#/components/schemas/ContactStatus"},
                    {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}},
                ]
            },
            "EmailValidationState": {
                "type": "object",
                "properties": {
                    "status": {"$ref": "#/components/schemas/EmailValidationStatus"},
                    "reason": {"type": "string"},
                    "validated_at": {"type": ["string", "null"], "format": "date-time"},
                },
            },
            "SubscriptionState": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "subscribed": {"type": "boolean"},
                    "status": {"type": ["string", "null"]},
                    "verified": {"type": "boolean"},
                    "verified_at": {"type": ["string", "null"], "format": "date-time"},
                    "unsubscribed_at": {"type": ["string", "null"], "format": "date-time"},
                    "unsubscribe_reason": {"type": "string"},
                },
            },
            "ContactListResponse": {
                "type": "object",
                "properties": {
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "count": {"type": "integer"},
                    "next_cursor": {"type": ["string", "null"], "description": "Pass as cursor for the next page."},
                    "contacts": {"type": "array", "items": {"$ref": "#/components/schemas/Contact"}},
                },
            },
            "ImportResult": {
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean"},
                    "idempotency_key": {"type": "string"},
                    "counts": {"type": "object", "additionalProperties": {"type": "integer"}},
                    "results": {"type": "array", "items": {"type": "object"}, "description": "Per-item/row actions."},
                    "errors": {"type": "array", "items": {"type": "object"}, "description": "Partial item/row validation errors."},
                },
            },
            "ContactHistory": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer"},
                    "email": {"type": "string"},
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "campaign_recipients": {"type": "array", "items": {"type": "object"}},
                    "transactional_messages": {"type": "array", "items": {"type": "object"}},
                    "events": {"type": "array", "items": {"type": "object"}},
                    "next_cursor": {"type": ["string", "null"]},
                },
            },
            "TransactionalSendRequest": {
                "type": "object",
                "required": ["email", "template_key"],
                "properties": {
                    "email": {"type": "string", "format": "email"},
                    "template_key": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "context": {"type": "object"},
                    "metadata": {"type": "object"},
                },
            },
            "TransactionalSendResponse": {
                "type": "object",
                "properties": {
                    "message": {"type": "object"},
                    "idempotent_replay": {"type": "boolean"},
                    "enqueued": {"type": "boolean"},
                },
            },
        },
    },
}
