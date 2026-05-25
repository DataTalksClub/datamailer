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

Use a local default base URL when running examples:

```bash
export DATAMAILER_URL="${DATAMAILER_URL:-http://127.0.0.1:8002}"
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

## Transactional Email API

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
