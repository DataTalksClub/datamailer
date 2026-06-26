import json
from copy import deepcopy

from django.conf import settings
from django.urls import reverse

from mailing.models import CampaignStatus, EmailValidationStatus, RecipientListType, SubscriptionStatus

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
    "mailing:api_contact_preferences": "/api/contacts/preferences",
    "mailing:api_contact_tags": "/api/contacts/{contact_id}/tags",
    "mailing:api_contact_tag": "/api/contacts/{contact_id}/tags/{tag_slug}",
    "mailing:api_contact_verification": "/api/contacts/{contact_id}/verification",
    "mailing:api_contact_validation": "/api/contacts/{contact_id}/validation",
    "mailing:api_contact_suppression": "/api/contacts/{contact_id}/suppression",
    "mailing:api_contact_history": "/api/contacts/{contact_id}/history",
    "mailing:api_client_senders": "/api/client/senders",
    "mailing:api_campaign": "/api/campaigns/{external_key}",
    "mailing:api_campaign_queue": "/api/campaigns/{external_key}/queue",
    "mailing:api_campaign_cancel": "/api/campaigns/{external_key}/cancel",
    "mailing:api_campaign_preview": "/api/campaigns/{external_key}/preview",
    "mailing:api_campaign_test_send": "/api/campaigns/{external_key}/test-send",
    "mailing:api_subscribe": "/api/subscriptions/subscribe",
    "mailing:api_unsubscribe": "/api/subscriptions/unsubscribe",
    "mailing:api_recipient_list": "/api/recipient-lists/{list_key}",
    "mailing:api_recipient_list_member": "/api/recipient-lists/{list_key}/members/{source_object_key}",
    "mailing:api_recipient_list_bulk_upsert": "/api/recipient-lists/{list_key}/members/bulk-upsert",
    "mailing:api_recipient_list_reconcile": "/api/recipient-lists/{list_key}/members/reconcile",
    "mailing:api_recipient_list_transactional_send": "/api/recipient-lists/{list_key}/transactional-send",
    "mailing:api_transient_recipient_list_transactional_send": (
        "/api/transient-recipient-lists/transactional-send"
    ),
    "mailing:api_transactional_template": "/api/transactional/templates/{template_key}",
    "mailing:api_transactional_send": "/api/transactional/send",
    "mailing:api_transactional_message_status": "/api/transactional/messages/{message_id}",
    "mailing:api_testbed_runs": "/api/testbed/runs",
    "mailing:api_testbed_run_detail": "/api/testbed/runs/{run_id}",
    "mailing:api_testbed_run_message": "/api/testbed/runs/{run_id}/messages/{message_id}",
    "mailing:tracking_open": "/t/o/{tracking_token}.gif",
    "mailing:tracking_click": "/t/c/{tracking_token}",
    "mailing:public_unsubscribe": "/unsubscribe/{unsubscribe_token}",
    "mailing:ses_webhook": "/webhooks/ses",
}


def code_json(value):
    return json.dumps(value, indent=2)


def docs_base_url():
    return settings.API_DOCS_BASE_URL.rstrip("/")


def contact_response(
    *, contact_id=101, email="learner@example.com", audience="dtc-courses", client="dtc-courses", tags=None
):
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


def workflow_examples(base_url=None):
    base_url = (base_url or docs_base_url()).rstrip("/")
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
                    "curl": f"""export DATAMAILER_URL="${{DATAMAILER_URL:-{base_url}}}"
export DATAMAILER_API_KEY="dm_dtccourses_demo_transactional_email_key"

curl -sS "$DATAMAILER_URL/api/contacts/status?email=alex.verified@example.com&audience=dtc-courses&client=dtc-courses" \\
  -H "Authorization: Bearer $DATAMAILER_API_KEY" """,
                    "python": """import os
import requests

base_url = os.getenv("DATAMAILER_URL", "__BASE_URL__")
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
response.raise_for_status()""".replace("__BASE_URL__", base_url),
                    "success": code_json(status_response()),
                    "error": code_json(
                        {
                            "error": {
                                "code": "invalid_api_key",
                                "message": "Authentication credentials were not accepted.",
                            }
                        }
                    ),
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
                    "request": code_json(
                        {"audience": "datatalks-club", "client": "dtc-newsletter", "tags": ["newsletter", "events"]}
                    ),
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
                            "from_email": "courses",
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
    "from_email": "courses",
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
        "from_email": "courses",
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
                                "from_email": "courses",
                                "from_email_address": "DataTalks.Club Courses <courses@dtcdev.click>",
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
                            "counts": {
                                "total": 1,
                                "created": 1,
                                "updated": 0,
                                "unchanged": 0,
                                "skipped": 0,
                                "invalid": 0,
                            },
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
                            "counts": {
                                "total": 1,
                                "created": 0,
                                "updated": 1,
                                "unchanged": 0,
                                "skipped": 0,
                                "invalid": 0,
                            },
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
                ("GET/PUT", "/api/contacts/preferences", "Read and update scoped category preferences."),
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
                ("GET", "/api/client/senders", "Get authenticated client sender policy."),
                ("PUT", "/api/client/senders", "Replace authenticated client sender policy."),
                ("POST", "/api/contacts/imports", "Bulk JSON import/upsert."),
                ("POST", "/api/contacts/imports/csv", "CSV upload/import."),
                ("PUT", "/api/transactional/templates/{template_key}", "Create or update a transactional template."),
                ("GET", "/api/transactional/templates/{template_key}", "Get one transactional template."),
                ("POST", "/api/transactional/send", "Queue one transactional email."),
                ("GET", "/api/transactional/messages/{message_id}", "Get transactional message status."),
            ],
        },
        {
            "name": "Campaigns",
            "endpoints": [
                ("PUT", "/api/campaigns/{external_key}", "Create or update one draft campaign."),
                ("GET", "/api/campaigns/{external_key}", "Get one campaign by external key."),
                ("POST", "/api/campaigns/{external_key}/queue", "Snapshot and queue one draft campaign."),
                ("POST", "/api/campaigns/{external_key}/cancel", "Cancel one draft or unsent queued campaign."),
                ("POST", "/api/campaigns/{external_key}/preview", "Render one campaign without recipients."),
                ("POST", "/api/campaigns/{external_key}/test-send", "Send one campaign to explicit test addresses."),
            ],
        },
        {
            "name": "Testbed",
            "endpoints": [
                ("GET", "/api/testbed/runs", "List captured rendered messages."),
                ("GET", "/api/testbed/runs/{run_id}", "Get one captured rendered message."),
                (
                    "GET",
                    "/api/testbed/runs/{run_id}/messages/{message_id}",
                    "Get the message for one captured run.",
                ),
                ("DELETE", "/api/testbed/runs", "Clear captured rendered messages."),
            ],
        },
        {
            "name": "Recipient Lists",
            "endpoints": [
                ("PUT", "/api/recipient-lists/{list_key}", "Create or update a client-scoped recipient list."),
                ("GET", "/api/recipient-lists/{list_key}", "Get recipient list metadata and counts."),
                (
                    "PUT",
                    "/api/recipient-lists/{list_key}/members/{source_object_key}",
                    "Create or update one list member.",
                ),
                (
                    "POST",
                    "/api/recipient-lists/{list_key}/members/bulk-upsert",
                    "Bulk create or update list members.",
                ),
                (
                    "POST",
                    "/api/recipient-lists/{list_key}/members/reconcile",
                    "Replace list membership from a current source snapshot.",
                ),
                (
                    "POST",
                    "/api/recipient-lists/{list_key}/transactional-send",
                    "Queue one transactional email per active list member.",
                ),
                (
                    "POST",
                    "/api/transient-recipient-lists/transactional-send",
                    "Queue one transactional email per active inline member without persisting a list.",
                ),
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
        API_DOC_PATHS["mailing:api_contact_preferences"]: reverse("mailing:api_contact_preferences"),
        API_DOC_PATHS["mailing:api_contact_tags"]: reverse("mailing:api_contact_tags", args=[123]),
        API_DOC_PATHS["mailing:api_contact_tag"]: reverse("mailing:api_contact_tag", args=[123, "newsletter"]),
        API_DOC_PATHS["mailing:api_contact_verification"]: reverse("mailing:api_contact_verification", args=[123]),
        API_DOC_PATHS["mailing:api_contact_validation"]: reverse("mailing:api_contact_validation", args=[123]),
        API_DOC_PATHS["mailing:api_contact_suppression"]: reverse("mailing:api_contact_suppression", args=[123]),
        API_DOC_PATHS["mailing:api_contact_history"]: reverse("mailing:api_contact_history", args=[123]),
        API_DOC_PATHS["mailing:api_client_senders"]: reverse("mailing:api_client_senders"),
        API_DOC_PATHS["mailing:api_campaign"]: reverse(
            "mailing:api_campaign",
            args=["cmp-course-start-2026"],
        ),
        API_DOC_PATHS["mailing:api_campaign_queue"]: reverse(
            "mailing:api_campaign_queue",
            args=["cmp-course-start-2026"],
        ),
        API_DOC_PATHS["mailing:api_campaign_cancel"]: reverse(
            "mailing:api_campaign_cancel",
            args=["cmp-course-start-2026"],
        ),
        API_DOC_PATHS["mailing:api_campaign_preview"]: reverse(
            "mailing:api_campaign_preview",
            args=["cmp-course-start-2026"],
        ),
        API_DOC_PATHS["mailing:api_campaign_test_send"]: reverse(
            "mailing:api_campaign_test_send",
            args=["cmp-course-start-2026"],
        ),
        API_DOC_PATHS["mailing:api_subscribe"]: reverse("mailing:api_subscribe"),
        API_DOC_PATHS["mailing:api_unsubscribe"]: reverse("mailing:api_unsubscribe"),
        API_DOC_PATHS["mailing:api_recipient_list"]: reverse(
            "mailing:api_recipient_list",
            args=["ml-zoomcamp-2026"],
        ),
        API_DOC_PATHS["mailing:api_recipient_list_member"]: reverse(
            "mailing:api_recipient_list_member",
            args=["ml-zoomcamp-2026:@e:@homework:homework-1", "homework-submission:42"],
        ),
        API_DOC_PATHS["mailing:api_recipient_list_bulk_upsert"]: reverse(
            "mailing:api_recipient_list_bulk_upsert",
            args=["ml-zoomcamp-2026"],
        ),
        API_DOC_PATHS["mailing:api_recipient_list_reconcile"]: reverse(
            "mailing:api_recipient_list_reconcile",
            args=["ml-zoomcamp-2026"],
        ),
        API_DOC_PATHS["mailing:api_recipient_list_transactional_send"]: reverse(
            "mailing:api_recipient_list_transactional_send",
            args=["ml-zoomcamp-2026:@e:@homework:homework-1"],
        ),
        API_DOC_PATHS["mailing:api_transient_recipient_list_transactional_send"]: reverse(
            "mailing:api_transient_recipient_list_transactional_send",
        ),
        API_DOC_PATHS["mailing:api_transactional_template"]: reverse(
            "mailing:api_transactional_template",
            args=["homework-submission-confirmation"],
        ),
        API_DOC_PATHS["mailing:api_transactional_send"]: reverse("mailing:api_transactional_send"),
        API_DOC_PATHS["mailing:api_transactional_message_status"]: reverse(
            "mailing:api_transactional_message_status",
            args=[123],
        ),
        API_DOC_PATHS["mailing:api_testbed_runs"]: reverse("mailing:api_testbed_runs"),
        API_DOC_PATHS["mailing:api_testbed_run_detail"]: reverse(
            "mailing:api_testbed_run_detail",
            args=[123],
        ),
        API_DOC_PATHS["mailing:api_testbed_run_message"]: reverse(
            "mailing:api_testbed_run_message",
            args=[123, 123],
        ),
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
RUN_ID_PARAM = {"name": "run_id", "in": "path", "required": True, "schema": {"type": "integer"}}
MESSAGE_ID_PARAM = {"name": "message_id", "in": "path", "required": True, "schema": {"type": "integer"}}
TAG_SLUG_PARAM = {"name": "tag_slug", "in": "path", "required": True, "schema": {"type": "string"}}
LIST_KEY_PARAM = {"name": "list_key", "in": "path", "required": True, "schema": {"type": "string"}}
CAMPAIGN_EXTERNAL_KEY_PARAM = {
    "name": "external_key",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "maxLength": 180},
}
SOURCE_OBJECT_KEY_PARAM = {
    "name": "source_object_key",
    "in": "path",
    "required": True,
    "schema": {"type": "string"},
}
TRACKING_PARAM = {"name": "tracking_token", "in": "path", "required": True, "schema": {"type": "string"}}
UNSUBSCRIBE_PARAM = {"name": "unsubscribe_token", "in": "path", "required": True, "schema": {"type": "string"}}

SCOPE_QUERY_PARAMS = [
    {"name": "email", "in": "query", "required": True, "schema": {"type": "string", "format": "email"}},
    {"name": "audience", "in": "query", "required": True, "schema": {"type": "string"}},
    {"name": "client", "in": "query", "required": True, "schema": {"type": "string"}},
]

SCOPE_ONLY_QUERY_PARAMS = [
    {"name": "audience", "in": "query", "required": True, "schema": {"type": "string"}},
    {"name": "client", "in": "query", "required": True, "schema": {"type": "string"}},
]

EXPORT_QUERY_PARAMS = [
    {"name": "audience", "in": "query", "required": True, "schema": {"type": "string"}},
    {"name": "client", "in": "query", "required": True, "schema": {"type": "string"}},
    {"name": "tags", "in": "query", "schema": {"type": "string", "description": "Comma-separated tag slugs."}},
    {"name": "subscription_status", "in": "query", "schema": {"$ref": "#/components/schemas/SubscriptionStatus"}},
    {"name": "verified", "in": "query", "schema": {"type": "boolean"}},
    {
        "name": "email_validation_status",
        "in": "query",
        "schema": {"$ref": "#/components/schemas/EmailValidationStatus"},
    },
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
        "description": "Local staff reference for implemented Datamailer endpoints. Client API routes use Bearer authentication with named Datamailer client API keys.",
    },
    "servers": [{"url": "/"}],
    "tags": [
        {"name": "Contacts"},
        {"name": "Subscriptions"},
        {"name": "Tags"},
        {"name": "State"},
        {"name": "Imports"},
        {"name": "Transactional"},
        {"name": "Testbed"},
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
                "responses": bearer_responses(
                    json_response("Contacts list", "#/components/schemas/ContactListResponse")
                ),
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
        "/api/contacts/preferences": {
            "get": {
                "tags": ["Contacts"],
                "summary": "Get category preferences",
                "security": [{"BearerAuth": []}],
                "parameters": SCOPE_QUERY_PARAMS
                + [
                    {
                        "name": "category_tags",
                        "in": "query",
                        "schema": {"type": "string"},
                        "description": "Comma-separated category tags to include with default enabled states.",
                    }
                ],
                "responses": bearer_responses(
                    json_response("Category preferences", "#/components/schemas/CategoryPreferencesResponse")
                ),
            },
            "put": {
                "tags": ["Contacts"],
                "summary": "Update category preferences",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/CategoryPreferencesUpdateRequest"),
                "responses": bearer_responses(
                    json_response("Category preferences", "#/components/schemas/CategoryPreferencesResponse")
                )
                | {"409": {"$ref": "#/components/responses/ValidationError"}},
            },
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
        "/api/campaigns/{external_key}": {
            "get": {
                "tags": ["Campaigns"],
                "summary": "Get campaign",
                "description": "Returns one campaign scoped to the authenticated client by external key.",
                "security": [{"BearerAuth": []}],
                "parameters": [CAMPAIGN_EXTERNAL_KEY_PARAM] + SCOPE_ONLY_QUERY_PARAMS,
                "responses": bearer_responses(json_response("Campaign", "#/components/schemas/CampaignResponse")),
            },
            "put": {
                "tags": ["Campaigns"],
                "summary": "Create or update draft campaign",
                "description": "Creates or updates one draft campaign scoped to the authenticated client by external key.",
                "security": [{"BearerAuth": []}],
                "parameters": [CAMPAIGN_EXTERNAL_KEY_PARAM],
                "requestBody": json_body("#/components/schemas/CampaignUpsertRequest"),
                "responses": bearer_responses(
                    json_response("Campaign", "#/components/schemas/CampaignUpsertResponse")
                )
                | {"201": json_response("Campaign created", "#/components/schemas/CampaignUpsertResponse")},
            },
        },
        "/api/campaigns/{external_key}/queue": {
            "post": {
                "tags": ["Campaigns"],
                "summary": "Queue draft campaign",
                "description": "Snapshots eligible contacts and queues one draft campaign for delivery.",
                "security": [{"BearerAuth": []}],
                "parameters": [CAMPAIGN_EXTERNAL_KEY_PARAM],
                "requestBody": json_body("#/components/schemas/ScopedMutationRequest"),
                "responses": bearer_responses(
                    json_response("Campaign queued", "#/components/schemas/CampaignQueueResponse"),
                    accepted=True,
                )
                | {"409": {"$ref": "#/components/responses/ValidationError"}},
            }
        },
        "/api/campaigns/{external_key}/cancel": {
            "post": {
                "tags": ["Campaigns"],
                "summary": "Cancel campaign",
                "description": "Cancels a draft campaign or a queued campaign before any recipient has been sent.",
                "security": [{"BearerAuth": []}],
                "parameters": [CAMPAIGN_EXTERNAL_KEY_PARAM],
                "requestBody": json_body("#/components/schemas/ScopedMutationRequest"),
                "responses": bearer_responses(
                    json_response("Campaign cancelled", "#/components/schemas/CampaignCancelResponse")
                )
                | {"409": {"$ref": "#/components/responses/ValidationError"}},
            }
        },
        "/api/campaigns/{external_key}/preview": {
            "post": {
                "tags": ["Campaigns"],
                "summary": "Preview campaign",
                "description": "Renders one campaign with tracking and unsubscribe markup but without recipients.",
                "security": [{"BearerAuth": []}],
                "parameters": [CAMPAIGN_EXTERNAL_KEY_PARAM],
                "requestBody": json_body("#/components/schemas/ScopedMutationRequest"),
                "responses": bearer_responses(
                    json_response("Campaign preview", "#/components/schemas/CampaignPreviewResponse")
                ),
            }
        },
        "/api/campaigns/{external_key}/test-send": {
            "post": {
                "tags": ["Campaigns"],
                "summary": "Test-send campaign",
                "description": "Sends one rendered campaign to explicit test addresses without creating campaign recipients or updating campaign delivery counts.",
                "security": [{"BearerAuth": []}],
                "parameters": [CAMPAIGN_EXTERNAL_KEY_PARAM],
                "requestBody": json_body("#/components/schemas/CampaignTestSendRequest"),
                "responses": bearer_responses(
                    json_response("Campaign test sent", "#/components/schemas/CampaignTestSendResponse"),
                    accepted=True,
                ),
            }
        },
        "/api/recipient-lists/{list_key}": {
            "get": {
                "tags": ["Recipient Lists"],
                "summary": "Get recipient list",
                "security": [{"BearerAuth": []}],
                "parameters": [
                    LIST_KEY_PARAM,
                    {"name": "audience", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "client", "in": "query", "required": True, "schema": {"type": "string"}},
                ],
                "responses": bearer_responses(
                    json_response("Recipient list", "#/components/schemas/RecipientListResponse")
                ),
            },
            "put": {
                "tags": ["Recipient Lists"],
                "summary": "Create or update recipient list",
                "security": [{"BearerAuth": []}],
                "parameters": [LIST_KEY_PARAM],
                "requestBody": json_body("#/components/schemas/RecipientListUpsertRequest"),
                "responses": bearer_responses(
                    json_response("Recipient list", "#/components/schemas/RecipientListUpsertResponse")
                ),
            },
        },
        "/api/recipient-lists/{list_key}/members/{source_object_key}": {
            "put": {
                "tags": ["Recipient Lists"],
                "summary": "Create or update recipient list member",
                "security": [{"BearerAuth": []}],
                "parameters": [LIST_KEY_PARAM, SOURCE_OBJECT_KEY_PARAM],
                "requestBody": json_body("#/components/schemas/RecipientListMemberUpsertRequest"),
                "responses": bearer_responses(
                    json_response("Recipient list member", "#/components/schemas/RecipientListMemberUpsertResponse")
                ),
            }
        },
        "/api/recipient-lists/{list_key}/members/bulk-upsert": {
            "post": {
                "tags": ["Recipient Lists"],
                "summary": "Bulk create or update recipient list members",
                "security": [{"BearerAuth": []}],
                "parameters": [LIST_KEY_PARAM],
                "requestBody": json_body("#/components/schemas/RecipientListBulkUpsertRequest"),
                "responses": bearer_responses(
                    json_response("Bulk recipient list result", "#/components/schemas/RecipientListBulkUpsertResponse")
                ),
            }
        },
        "/api/recipient-lists/{list_key}/members/reconcile": {
            "post": {
                "tags": ["Recipient Lists"],
                "summary": "Reconcile recipient list members",
                "description": "Applies a current source snapshot and can mark absent active members removed.",
                "security": [{"BearerAuth": []}],
                "parameters": [LIST_KEY_PARAM],
                "requestBody": json_body("#/components/schemas/RecipientListReconcileRequest"),
                "responses": bearer_responses(
                    json_response(
                        "Recipient list reconcile result", "#/components/schemas/RecipientListReconcileResponse"
                    )
                ),
            }
        },
        "/api/recipient-lists/{list_key}/transactional-send": {
            "post": {
                "tags": ["Recipient Lists"],
                "summary": "Send transactional email to recipient list",
                "description": "Optionally reconciles or upserts list members, then creates one transactional message per active list member. The base idempotency key is expanded with each member source object key. Member metadata is merged into each recipient context and is also available under the member key.",
                "security": [{"BearerAuth": []}],
                "parameters": [LIST_KEY_PARAM],
                "requestBody": json_body("#/components/schemas/RecipientListTransactionalSendRequest"),
                "responses": bearer_responses(
                    json_response(
                        "Recipient list transactional send result",
                        "#/components/schemas/RecipientListTransactionalSendResponse",
                    ),
                    accepted=True,
                ),
            }
        },
        "/api/transient-recipient-lists/transactional-send": {
            "post": {
                "tags": ["Recipient Lists"],
                "summary": "Send transactional email to transient recipient list",
                "description": "Creates one transactional message per active inline member without creating recipient-list or membership records. The base idempotency key is expanded with each member source object key. Member metadata is merged into each recipient context and is also available under the member key.",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/TransientRecipientListTransactionalSendRequest"),
                "responses": bearer_responses(
                    json_response(
                        "Transient recipient list transactional send result",
                        "#/components/schemas/TransientRecipientListTransactionalSendResponse",
                    ),
                    accepted=True,
                ),
            }
        },
        "/api/client/senders": {
            "get": {
                "tags": ["Transactional"],
                "summary": "Get client sender policy",
                "description": "Returns configured sender IDs and display addresses for the authenticated client.",
                "security": [{"BearerAuth": []}],
                "responses": bearer_responses(
                    json_response("Client sender policy", "#/components/schemas/ClientSenderPolicy")
                ),
            },
            "put": {
                "tags": ["Transactional"],
                "summary": "Replace client sender policy",
                "description": "Replaces configured sender IDs and the default sender for the authenticated client. The sender email may include an RFC 5322 display name, for example DataTalks.Club Courses <courses@dtcdev.click>.",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/ClientSenderPolicyUpdateRequest"),
                "responses": bearer_responses(
                    json_response("Client sender policy", "#/components/schemas/ClientSenderPolicy")
                ),
            },
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
        "/api/transactional/templates/{template_key}": {
            "get": {
                "tags": ["Transactional"],
                "summary": "Get transactional template",
                "description": "Returns one active or inactive transactional template scoped to the authenticated client.",
                "security": [{"BearerAuth": []}],
                "parameters": [
                    {
                        "name": "template_key",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": bearer_responses(
                    json_response("Transactional template", "#/components/schemas/TransactionalTemplate")
                ),
            },
            "put": {
                "tags": ["Transactional"],
                "summary": "Create or update transactional template",
                "description": "Creates or updates one transactional template scoped to the authenticated client.",
                "security": [{"BearerAuth": []}],
                "parameters": [
                    {
                        "name": "template_key",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "requestBody": json_body("#/components/schemas/TransactionalTemplateUpsertRequest"),
                "responses": bearer_responses(
                    json_response("Transactional template", "#/components/schemas/TransactionalTemplateUpsertResponse")
                ),
            },
        },
        "/api/transactional/messages/{message_id}": {
            "get": {
                "tags": ["Transactional"],
                "summary": "Get transactional message status",
                "description": "Returns the current status and event timeline for a transactional message created by the authenticated client.",
                "security": [{"BearerAuth": []}],
                "parameters": [
                    {
                        "name": "message_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer", "minimum": 1},
                    }
                ],
                "responses": bearer_responses(
                    json_response(
                        "Transactional message status",
                        "#/components/schemas/TransactionalMessageStatusResponse",
                    )
                )
                | {"404": {"$ref": "#/components/responses/ValidationError"}},
            }
        },
        "/api/testbed/runs": {
            "get": {
                "tags": ["Testbed"],
                "summary": "List captured runs",
                "description": "Lists rendered messages captured while delivery mode is capture.",
                "security": [{"BearerAuth": []}],
                "parameters": [
                    {"name": "email", "in": "query", "schema": {"type": "string", "format": "email"}},
                    {"name": "source", "in": "query", "schema": {"type": "string"}},
                    {"name": "event", "in": "query", "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                ],
                "responses": bearer_responses(
                    json_response("Captured runs", "#/components/schemas/CapturedRunList")
                ),
            },
            "delete": {
                "tags": ["Testbed"],
                "summary": "Clear captured runs",
                "description": "Deletes captured rendered messages for the authenticated client.",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/CapturedRunClearRequest", required=False),
                "responses": bearer_responses(
                    json_response("Captured runs cleared", "#/components/schemas/CapturedRunClearResponse")
                ),
            },
        },
        "/api/testbed/runs/{run_id}": {
            "get": {
                "tags": ["Testbed"],
                "summary": "Get captured run",
                "security": [{"BearerAuth": []}],
                "parameters": [RUN_ID_PARAM],
                "responses": bearer_responses(
                    json_response("Captured run", "#/components/schemas/CapturedRunResponse")
                )
                | {"404": {"$ref": "#/components/responses/ValidationError"}},
            }
        },
        "/api/testbed/runs/{run_id}/messages/{message_id}": {
            "get": {
                "tags": ["Testbed"],
                "summary": "Get captured run message",
                "security": [{"BearerAuth": []}],
                "parameters": [RUN_ID_PARAM, MESSAGE_ID_PARAM],
                "responses": bearer_responses(
                    json_response("Captured run message", "#/components/schemas/CapturedRunMessageResponse")
                )
                | {"404": {"$ref": "#/components/responses/ValidationError"}},
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
                "responses": {
                    "200": {"description": "HTML confirmation"},
                    "400": {"description": "HTML validation error"},
                },
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
            "CampaignStatus": {"type": "string", "enum": [choice.value for choice in CampaignStatus]},
            "CampaignUpsertRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["subject"],
                        "properties": {
                            "subject": {"type": "string", "maxLength": 255},
                            "preview_text": {"type": "string", "maxLength": 255},
                            "html_body": {"type": "string"},
                            "text_body": {"type": "string"},
                            "scheduled_at": {"type": ["string", "null"], "format": "date-time"},
                            "include_tags": {"type": "array", "items": {"type": "string"}},
                            "exclude_tags": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                ]
            },
            "Campaign": {
                "type": "object",
                "properties": {
                    "external_key": {"type": "string"},
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "subject": {"type": "string"},
                    "preview_text": {"type": "string"},
                    "html_body": {"type": "string"},
                    "text_body": {"type": "string"},
                    "status": {"$ref": "#/components/schemas/CampaignStatus"},
                    "scheduled_at": {"type": ["string", "null"], "format": "date-time"},
                    "sent_at": {"type": ["string", "null"], "format": "date-time"},
                    "include_tags": {"type": "array", "items": {"type": "string"}},
                    "exclude_tags": {"type": "array", "items": {"type": "string"}},
                    "recipient_count": {"type": "integer"},
                    "sent_count": {"type": "integer"},
                    "skipped_count": {"type": "integer"},
                    "delivered_count": {"type": "integer"},
                    "unique_open_count": {"type": "integer"},
                    "open_count": {"type": "integer"},
                    "unique_click_count": {"type": "integer"},
                    "click_count": {"type": "integer"},
                    "unsubscribe_count": {"type": "integer"},
                    "bounce_count": {"type": "integer"},
                    "complaint_count": {"type": "integer"},
                    "created_at": {"type": "string", "format": "date-time"},
                    "updated_at": {"type": "string", "format": "date-time"},
                },
            },
            "CampaignResponse": {
                "type": "object",
                "properties": {"campaign": {"$ref": "#/components/schemas/Campaign"}},
            },
            "CampaignUpsertResponse": {
                "type": "object",
                "properties": {
                    "campaign": {"$ref": "#/components/schemas/Campaign"},
                    "created": {"type": "boolean"},
                },
            },
            "CampaignQueueResponse": {
                "type": "object",
                "properties": {
                    "campaign": {"$ref": "#/components/schemas/Campaign"},
                    "queued": {"type": "boolean"},
                    "batch_count": {"type": "integer"},
                    "recipient_count": {"type": "integer"},
                    "skipped_count": {"type": "integer"},
                },
            },
            "CampaignCancelResponse": {
                "type": "object",
                "properties": {
                    "campaign": {"$ref": "#/components/schemas/Campaign"},
                    "cancelled": {"type": "boolean"},
                    "skipped_count": {"type": "integer"},
                },
            },
            "CampaignPreview": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "preview_text": {"type": "string"},
                    "html_body": {"type": "string"},
                    "text_body": {"type": "string"},
                },
            },
            "CampaignPreviewResponse": {
                "type": "object",
                "properties": {
                    "campaign": {"$ref": "#/components/schemas/Campaign"},
                    "preview": {"$ref": "#/components/schemas/CampaignPreview"},
                },
            },
            "CampaignTestSendRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["emails"],
                        "properties": {
                            "emails": {
                                "type": "array",
                                "items": {"type": "string", "format": "email"},
                                "minItems": 1,
                                "maxItems": 25,
                            }
                        },
                    },
                ]
            },
            "CampaignTestSendResponse": {
                "type": "object",
                "properties": {
                    "campaign": {"$ref": "#/components/schemas/Campaign"},
                    "sent_count": {"type": "integer"},
                    "recipients": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "email": {"type": "string", "format": "email"},
                                "message_id": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "CapturedRun": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "email": {"type": "string", "format": "email"},
                    "from_email": {"type": "string"},
                    "subject": {"type": "string"},
                    "template_key": {"type": "string"},
                    "source": {"type": "string"},
                    "event": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "created_at": {"type": "string", "format": "date-time"},
                },
            },
            "CapturedRunDetail": {
                "allOf": [
                    {"$ref": "#/components/schemas/CapturedRun"},
                    {
                        "type": "object",
                        "properties": {
                            "html_body": {"type": "string"},
                            "text_body": {"type": "string"},
                            "metadata": {"type": "object"},
                            "transactional_message_id": {"type": ["integer", "null"]},
                            "campaign_id": {"type": ["integer", "null"]},
                            "campaign_recipient_id": {"type": ["integer", "null"]},
                        },
                    },
                ]
            },
            "CapturedRunList": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "runs": {"type": "array", "items": {"$ref": "#/components/schemas/CapturedRun"}},
                },
            },
            "CapturedRunResponse": {
                "type": "object",
                "properties": {"run": {"$ref": "#/components/schemas/CapturedRunDetail"}},
            },
            "CapturedRunMessageResponse": {
                "type": "object",
                "properties": {"message": {"$ref": "#/components/schemas/CapturedRunDetail"}},
            },
            "CapturedRunClearRequest": {
                "type": "object",
                "properties": {"email": {"type": "string", "format": "email"}},
            },
            "CapturedRunClearResponse": {
                "type": "object",
                "properties": {"deleted_count": {"type": "integer"}},
            },
            "RecipientListType": {"type": "string", "enum": [choice.value for choice in RecipientListType]},
            "RecipientListInput": {
                "type": "object",
                "properties": {
                    "type": {"$ref": "#/components/schemas/RecipientListType"},
                    "name": {"type": "string"},
                    "metadata": {"type": "object"},
                },
            },
            "RecipientListMemberInput": {
                "type": "object",
                "required": ["email"],
                "properties": {
                    "source_object_key": {"type": "string"},
                    "email": {"type": "string", "format": "email"},
                    "status": {"type": "string", "enum": ["active", "removed"]},
                    "metadata": {"type": "object"},
                },
            },
            "RecipientListUpsertRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {"$ref": "#/components/schemas/RecipientListInput"},
                ]
            },
            "RecipientListMemberUpsertRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "properties": {
                            "list": {"$ref": "#/components/schemas/RecipientListInput"},
                            "member": {"$ref": "#/components/schemas/RecipientListMemberInput"},
                        },
                    },
                ]
            },
            "RecipientListBulkUpsertRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["members"],
                        "properties": {
                            "list": {"$ref": "#/components/schemas/RecipientListInput"},
                            "members": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/RecipientListMemberInput"},
                            },
                        },
                    },
                ]
            },
            "RecipientListReconcileRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/RecipientListBulkUpsertRequest"},
                    {
                        "type": "object",
                        "properties": {
                            "dry_run": {"type": "boolean"},
                            "remove_absent": {"type": "boolean"},
                        },
                    },
                ]
            },
            "RecipientListTransactionalSendRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["template_key", "idempotency_key"],
                        "properties": {
                            "template_key": {"type": "string"},
                            "idempotency_key": {"type": "string"},
                            "category_tag": {"type": "string"},
                            "context": {"type": "object"},
                            "metadata": {"type": "object"},
                            "list": {"$ref": "#/components/schemas/RecipientListInput"},
                            "members": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/RecipientListMemberInput"},
                            },
                            "member_sync": {
                                "type": "string",
                                "enum": ["reconcile", "upsert"],
                                "default": "reconcile",
                            },
                            "remove_absent_members": {
                                "type": "boolean",
                                "default": True,
                            },
                            "from_email": {"type": "string"},
                            "reply_to": {"type": "string", "format": "email"},
                            "cc": {
                                "type": "array",
                                "items": {"type": "string", "format": "email"},
                            },
                            "bcc": {
                                "type": "array",
                                "items": {"type": "string", "format": "email"},
                            },
                        },
                    },
                ]
            },
            "TransientRecipientListTransactionalSendRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["template_key", "idempotency_key", "members"],
                        "properties": {
                            "template_key": {"type": "string"},
                            "idempotency_key": {"type": "string"},
                            "category_tag": {"type": "string"},
                            "context": {"type": "object"},
                            "metadata": {"type": "object"},
                            "list": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string"},
                                    "name": {"type": "string"},
                                    "metadata": {"type": "object"},
                                },
                            },
                            "members": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/RecipientListMemberInput"},
                            },
                            "from_email": {"type": "string"},
                            "reply_to": {"type": "string", "format": "email"},
                            "cc": {
                                "type": "array",
                                "items": {"type": "string", "format": "email"},
                            },
                            "bcc": {
                                "type": "array",
                                "items": {"type": "string", "format": "email"},
                            },
                        },
                    },
                ]
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
            "CategoryPreference": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "label": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
            },
            "CategoryPreferencesUpdateRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["email", "categories"],
                        "properties": {
                            "email": {"type": "string", "format": "email"},
                            "categories": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/CategoryPreference"},
                            },
                        },
                    },
                ]
            },
            "CategoryPreferencesResponse": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "format": "email"},
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "categories": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/CategoryPreference"},
                    },
                    "global_unsubscribed": {"type": "boolean"},
                    "suppressed": {"type": "boolean"},
                    "suppression_reasons": {"type": "array", "items": {"type": "string"}},
                },
            },
            "RecipientList": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "type": {"$ref": "#/components/schemas/RecipientListType"},
                    "name": {"type": "string"},
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "metadata": {"type": "object"},
                    "member_count": {"type": "integer"},
                    "active_member_count": {"type": "integer"},
                    "last_reconciled_at": {"type": ["string", "null"], "format": "date-time"},
                    "created_at": {"type": ["string", "null"], "format": "date-time"},
                    "updated_at": {"type": ["string", "null"], "format": "date-time"},
                },
            },
            "RecipientListMember": {
                "type": "object",
                "properties": {
                    "source_object_key": {"type": "string"},
                    "email": {"type": "string", "format": "email"},
                    "contact_id": {"type": "integer"},
                    "status": {"type": "string", "enum": ["active", "removed"]},
                    "active": {"type": "boolean"},
                    "removed_at": {"type": ["string", "null"], "format": "date-time"},
                    "metadata": {"type": "object"},
                    "created_at": {"type": "string", "format": "date-time"},
                    "updated_at": {"type": "string", "format": "date-time"},
                },
            },
            "RecipientListResponse": {
                "type": "object",
                "properties": {"recipient_list": {"$ref": "#/components/schemas/RecipientList"}},
            },
            "RecipientListUpsertResponse": {
                "type": "object",
                "properties": {
                    "recipient_list": {"$ref": "#/components/schemas/RecipientList"},
                    "created": {"type": "boolean"},
                },
            },
            "RecipientListMemberUpsertResponse": {
                "type": "object",
                "properties": {
                    "recipient_list": {"$ref": "#/components/schemas/RecipientList"},
                    "member": {"$ref": "#/components/schemas/RecipientListMember"},
                    "created": {"type": "boolean"},
                },
            },
            "RecipientListBulkUpsertResponse": {
                "type": "object",
                "properties": {
                    "recipient_list": {"$ref": "#/components/schemas/RecipientList"},
                    "created_count": {"type": "integer"},
                    "updated_count": {"type": "integer"},
                },
            },
            "RecipientListReconcileResponse": {
                "type": "object",
                "properties": {
                    "recipient_list": {"$ref": "#/components/schemas/RecipientList"},
                    "dry_run": {"type": "boolean"},
                    "upsert_count": {"type": "integer"},
                    "removed_count": {"type": "integer"},
                },
            },
            "RecipientListTransactionalSendResponse": {
                "type": "object",
                "properties": {
                    "recipient_list": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "active_member_count": {"type": "integer"},
                        },
                    },
                    "template_key": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "created_count": {"type": "integer"},
                    "enqueued_count": {"type": "integer"},
                    "skipped_count": {"type": "integer"},
                    "idempotent_replay_count": {"type": "integer"},
                },
            },
            "TransientRecipientListTransactionalSendResponse": {
                "type": "object",
                "properties": {
                    "transient_recipient_list": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "name": {"type": "string"},
                            "member_count": {"type": "integer"},
                            "active_member_count": {"type": "integer"},
                        },
                    },
                    "template_key": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "created_count": {"type": "integer"},
                    "enqueued_count": {"type": "integer"},
                    "skipped_count": {"type": "integer"},
                    "idempotent_replay_count": {"type": "integer"},
                },
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
                    "errors": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Partial item/row validation errors.",
                    },
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
            "Sender": {
                "type": "object",
                "required": ["id", "email"],
                "properties": {
                    "id": {"type": "string", "description": "Configured sender ID used in from_email payloads."},
                    "email": {
                        "type": "string",
                        "description": "Sender address, optionally with display name.",
                        "examples": ["DataTalks.Club Courses <courses@dtcdev.click>"],
                    },
                },
            },
            "ClientSenderPolicy": {
                "type": "object",
                "properties": {
                    "client": {"type": "object"},
                    "default_sender_id": {"type": "string"},
                    "senders": {"type": "array", "items": {"$ref": "#/components/schemas/Sender"}},
                },
            },
            "ClientSenderPolicyUpdateRequest": {
                "type": "object",
                "required": ["default_sender_id", "senders"],
                "properties": {
                    "default_sender_id": {"type": "string"},
                    "senders": {"type": "array", "items": {"$ref": "#/components/schemas/Sender"}},
                },
            },
            "TransactionalSendRequest": {
                "type": "object",
                "required": ["email", "template_key"],
                "properties": {
                    "email": {"type": "string", "format": "email"},
                    "from_email": {
                        "type": "string",
                        "description": "Optional configured sender ID, such as courses. If omitted, Datamailer uses the client's default sender ID. Raw email addresses are rejected.",
                    },
                    "reply_to": {"type": "string", "format": "email"},
                    "cc": {
                        "type": "array",
                        "items": {"type": "string", "format": "email"},
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string", "format": "email"},
                    },
                    "template_key": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "category_tag": {"type": "string"},
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
            "TransactionalTemplateUpsertRequest": {
                "type": "object",
                "required": ["name", "subject"],
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "subject": {"type": "string"},
                    "html_body": {"type": "string"},
                    "text_body": {"type": "string"},
                    "required_context": {"type": "array", "items": {"type": "object"}},
                    "example_context": {"type": "object"},
                    "is_active": {"type": "boolean"},
                },
            },
            "TransactionalTemplate": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "client": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "subject": {"type": "string"},
                    "html_body": {"type": "string"},
                    "text_body": {"type": "string"},
                    "required_context": {"type": "array", "items": {"type": "object"}},
                    "example_context": {"type": "object"},
                    "is_transactional": {"type": "boolean"},
                    "is_active": {"type": "boolean"},
                },
            },
            "TransactionalTemplateUpsertResponse": {
                "type": "object",
                "properties": {
                    "template": {"$ref": "#/components/schemas/TransactionalTemplate"},
                    "created": {"type": "boolean"},
                },
            },
            "TransactionalMessageStatusResponse": {
                "type": "object",
                "properties": {
                    "message": {"type": "object"},
                    "events": {"type": "array", "items": {"type": "object"}},
                },
            },
        },
    },
}
