# API Design

Datamailer client apps use the native API to sync contacts, check subscription and verification state, import/export contacts, send transactional emails, and retrieve scoped contact history.

The in-app staff API docs at `/api-docs/` are the primary runnable reference. They include copy-pasteable local examples, request/response bodies, common errors, and the endpoint reference generated alongside OpenAPI JSON.

## Authentication

All client API endpoints under `/api/...` require Bearer authentication:

```text
Authorization: Bearer <client-api-key>
```

Clients can have multiple named API keys for separate integrations. Datamailer stores only key hashes and displays each key's safe `dm_<prefix>` identifier for support and audit trails. Staff users create and revoke keys from the client detail page in the operator UI.

Local demo data creates stable named keys for examples:

- `dtc-courses` / `Course platform transactional`: `dm_dtccourses_demo_transactional_email_key`
- `dtc-newsletter` / `Newsletter import/export`: `dm_dtcnews_demo_newsletter_import_export_key`
- `asl-platform` / `ASL platform transactional`: `dm_aslplatform_demo_transactional_email_key`

The in-app examples default to `DATAMAILER_API_DOCS_BASE_URL`, falling back to `PUBLIC_BASE_URL`. Override `DATAMAILER_URL` when running an example against a different environment:

```bash
export DATAMAILER_URL="${DATAMAILER_URL:-https://datamailer.example.com}"
export DATAMAILER_API_KEY="dm_dtccourses_demo_transactional_email_key"
```

## Contact APIs

### Upsert Contact

```text
POST /api/contacts
```

```json
{
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
}
```

Creates or updates the global contact, creates or updates the audience/client subscription, and adds audience-scoped tags.

`verified=true` marks the audience/client subscription as verified for the authenticated client scope. Marketing, campaign, and recipient-list eligibility treat a contact as verified when the global contact, audience subscription, or client subscription has a verification timestamp.

### Contact Status

```text
GET /api/contacts/status?email=learner@example.com&audience=dtc-courses&client=dtc-courses
```

Returns contact existence, subscription, verification, validation, suppression, and sendability state for the authenticated client scope.

### Verification, Validation, and Suppression

```text
PATCH /api/contacts/{contact_id}/verification
PATCH /api/contacts/{contact_id}/validation
PATCH /api/contacts/{contact_id}/suppression
```

These endpoints update state for an existing contact visible to the authenticated client scope. Verification is typically called after the user verifies in the client app. Validation stores external hygiene decisions. Suppression records global unsubscribe, hard bounce, or complaint state.

### Tags

```text
PUT /api/contacts/{contact_id}/tags
POST /api/contacts/{contact_id}/tags/{tag_slug}
DELETE /api/contacts/{contact_id}/tags/{tag_slug}
```

Use `PUT` to replace the audience tag set. Use single-tag `POST` and `DELETE` for toggle-style client workflows.

## Subscription APIs

```text
POST /api/subscriptions/subscribe
POST /api/subscriptions/unsubscribe
```

Subscribe creates the contact if needed and marks the client-scoped subscription subscribed.

Unsubscribe accepts `scope` values:

- `client`: unsubscribe from one client.
- `audience`: unsubscribe from the whole audience.
- `global`: unsubscribe from all marketing email managed by Datamailer.

## Import and Export APIs

```text
POST /api/contacts/imports
POST /api/contacts/imports/csv
GET /api/contacts
GET /api/contacts.csv
```

JSON and CSV imports are idempotent by normalized email plus audience/client scope. Invalid items are returned in partial errors while valid items continue. CSV export returns safe recreatable contact, subscription, tag, verification, validation, suppression, unsubscribe, and update timestamp columns.

## Recipient List APIs

Recipient lists are client-scoped batches for later list sends. CMP uses them for groups such as course registrants, homework submitters, and project submitters. Keys are unique within the authenticated client plus audience scope, for example `registrants:ml-zoomcamp-2026` or `homework-submitters:ml-zoomcamp-2026:homework-1`.

```text
PUT /api/recipient-lists/{list_key}
GET /api/recipient-lists/{list_key}
PUT /api/recipient-lists/{list_key}/members/{source_object_key}
POST /api/recipient-lists/{list_key}/members/bulk-upsert
POST /api/recipient-lists/{list_key}/members/reconcile
POST /api/recipient-lists/{list_key}/transactional-send
```

Single member upsert creates the parent list when it does not exist, so CMP does not need a separate "does this batch exist?" call when the first learner submits:

```json
{
  "audience": "dtc-courses",
  "client": "dtc-courses",
  "list": {
    "type": "homework_submitters",
    "name": "ML Zoomcamp 2026 Homework 1 submitters",
    "metadata": {
      "course": "ml-zoomcamp-2026",
      "homework": "homework-1"
    }
  },
  "member": {
    "email": "learner@example.com",
    "status": "active",
    "metadata": {
      "submission_id": 42
    }
  }
}
```

Bulk upsert accepts the same scope and list metadata plus a `members` array. It is intended for retroactive creation from CMP:

```json
{
  "audience": "dtc-courses",
  "client": "dtc-courses",
  "list": {
    "type": "registrants",
    "name": "ML Zoomcamp 2026 registrants"
  },
  "members": [
    {
      "source_object_key": "registration:1",
      "email": "one@example.com",
      "metadata": {"user_id": 1}
    },
    {
      "source_object_key": "registration:2",
      "email": "two@example.com",
      "metadata": {"user_id": 2}
    }
  ]
}
```

Reconcile uses the provided member array as the current desired list. With `remove_absent=true`, existing members missing from the payload are marked removed instead of deleted. This gives CMP an idempotent backfill path for "everyone registered" and "everyone who submitted this homework/project" lists.

List transactional send creates one transactional message per active list member using a shared template and context. The caller must provide a base `idempotency_key`; Datamailer appends each member `source_object_key` so retrying the same list send does not duplicate per-member email:

```json
{
  "audience": "dtc-courses",
  "client": "dtc-courses",
  "template_key": "homework-score-published",
  "idempotency_key": "homework-score:ml-zoomcamp-2026:homework-1",
  "context": {
    "course_title": "ML Zoomcamp 2026",
    "homework_title": "Homework 1",
    "scores_url": "https://courses.example.com/courses/ml-zoomcamp-2026/"
  },
  "metadata": {
    "source": "course-management-platform",
    "event": "homework_score_publication"
  }
}
```

## Transactional Email API

### Transactional Template Upsert

```text
PUT /api/transactional/templates/{template_key}
GET /api/transactional/templates/{template_key}
```

Templates are scoped to the authenticated client. Use `PUT` to create or update a transactional template, and `GET` to verify the current configuration.

```json
{
  "name": "Homework Submission Confirmation",
  "description": "Confirm that the course platform saved a homework submission.",
  "subject": "Homework submission received: {{ homework_title }}",
  "html_body": "<p>Your homework submission for <strong>{{ homework_title }}</strong> in {{ course_title }} was saved.</p>",
  "text_body": "Your homework submission for {{ homework_title }} in {{ course_title }} was saved.",
  "required_context": [
    {"name": "course_title", "description": "Course title."},
    {"name": "homework_title", "description": "Homework title."}
  ],
  "example_context": {
    "course_title": "ML Zoomcamp",
    "homework_title": "Homework 1"
  },
  "is_active": true
}
```

The CMP homework confirmation template can be provisioned through the API with:

```bash
DATAMAILER_URL="https://datamailer.example.com" \
DATAMAILER_API_KEY="<client-api-key>" \
python scripts/upsert_homework_confirmation_template.py
```

### Send Transactional Email

```text
POST /api/transactional/send
```

```json
{
  "email": "learner@example.com",
  "template_key": "registration-welcome",
  "idempotency_key": "registration-user-123",
  "context": {
    "name": "Learner",
    "course_name": "ML Zoomcamp"
  },
  "metadata": {
    "source": "registration"
  }
}
```

Transactional sends validate required template context before Datamailer creates a contact, message, event, or queue payload. Reusing an idempotency key returns the existing message.

Local transactional send examples require the Datamailer server to be started with `SQS_TRANSACTIONAL_EMAIL_QUEUE_URL` configured, for example through LocalStack. With the default empty queue URL, the endpoint is documented but is not runnable because queueing provider work will fail.

Transactional sends do not require marketing subscription, but they are blocked for hard bounces and complaints.

### Transactional Message Status

```text
GET /api/transactional/messages/{message_id}
```

Returns the current status for a transactional message created by the authenticated client, plus its event timeline. Use the `message.id` returned by `POST /api/transactional/send`.

## Contact History

```text
GET /api/contacts/{contact_id}/history?audience=dtc-courses&client=dtc-courses&limit=25
```

Returns safe scoped campaign recipient, transactional message, and event history. Secret hashes and delivery link tokens are never returned.

## Public and Provider Routes

Public tracking, unsubscribe, and provider webhook routes are documented in OpenAPI and the in-app endpoint reference for integration context:

```text
GET /t/o/{tracking_token}.gif
GET /t/c/{tracking_token}
GET /unsubscribe/{unsubscribe_token}
POST /unsubscribe/{unsubscribe_token}
POST /webhooks/ses
```

These are not client Bearer API routes.

## CMP Contact Event Callbacks

When configured, Datamailer posts hard-bounce, complaint, and public
unsubscribe events back to CMP after the local Datamailer transaction commits.

```text
CMP_WEBHOOK_URL=https://courses.example.com/api/datamailer/events
CMP_WEBHOOK_TOKEN=shared-secret
```

Datamailer sends:

```text
Authorization: Bearer <CMP_WEBHOOK_TOKEN>
```

Implemented callback event types:

```text
contact.hard_bounced
contact.complained
subscription.unsubscribed
```
