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

Sandbox deployment provisions the CMP scope explicitly:

```bash
python manage.py provision_client_scope \
  --organization datatalksclub \
  --organization-name DataTalksClub \
  --audience dtc-courses \
  --audience-name "DataTalksClub Courses" \
  --client dtc-courses \
  --client-name "DTC Courses"

python manage.py set_client_senders dtc-courses \
  --organization datatalksclub \
  --default-sender courses \
  --sender 'courses=DataTalks.Club Courses <courses@dtcdev.click>'
```

That keeps `DATAMAILER_AUDIENCE=dtc-courses` and
`DATAMAILER_CLIENT=dtc-courses` valid for CMP contact sync, status lookups,
history lookups, recipient lists, and transactional sends. The sender command
keeps CMP payloads using `from_email=courses` while making delivered mail show
`DataTalks.Club Courses`.

The same sender mapping is available through the client-scoped API:

```bash
curl -sS -X PUT "$DATAMAILER_URL/api/client/senders" \
  -H "Authorization: Bearer $DATAMAILER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "default_sender_id": "courses",
    "senders": [
      {
        "id": "courses",
        "email": "DataTalks.Club Courses <courses@dtcdev.click>"
      }
    ]
  }'
```

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

Recipient lists are client-scoped audience nodes for later list sends. CMP writes path keys such as `ml-zoomcamp-2026`, `ml-zoomcamp-2026:@e`, and `ml-zoomcamp-2026:@e:@homework:homework-1`. Adding a member to a child path also adds cascade memberships to each parent path, including `<all>`. CMP writes the most specific node, and Datamailer keeps the ancestors current.

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

List transactional send creates one transactional message per active list member using a shared template and context. The caller must provide a base `idempotency_key`; Datamailer appends each member `source_object_key` so retrying the same list send does not duplicate per-member email.

The request can include `list` and `members`. When `members` is present, Datamailer syncs the recipient list before sending. The default `member_sync` mode is `reconcile`, which marks existing active members absent from the request as removed. Use `member_sync=upsert` to only add or update the provided members. Each member's metadata is merged into that recipient's template context and is also available under `member`.

```json
{
  "audience": "dtc-courses",
  "client": "dtc-courses",
  "template_key": "homework-score-notification",
  "idempotency_key": "homework-score:ml-zoomcamp-2026:homework-1",
  "context": {
    "course_title": "ML Zoomcamp 2026",
    "homework_title": "Homework 1",
    "scores_url": "https://courses.example.com/courses/ml-zoomcamp-2026/"
  },
  "list": {
    "type": "homework_submitters",
    "name": "ML Zoomcamp 2026 Homework 1 submitters"
  },
  "members": [
    {
      "source_object_key": "homework-submission:123",
      "email": "learner@example.com",
      "status": "active",
      "metadata": {
        "submission_id": 123,
        "questions_score": 6,
        "learning_in_public_score": 2,
        "faq_score": 1,
        "total_score": 9
      }
    }
  ],
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

CMP transactional templates can be provisioned through the API with:

```bash
DATAMAILER_URL="https://datamailer.example.com" \
DATAMAILER_API_KEY="<client-api-key>" \
python scripts/upsert_cmp_templates.py
```

The script provisions these template keys:

```text
homework-submission-confirmation
project-submission-confirmation
homework-score-notification
project-score-notification
certificate-availability-notification
deadline-reminder
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

## Transactional Dry-Run (test-only)

```text
POST /api/transactional/send      { ..., "dry_run": true }
```

For end-to-end tests, the real send endpoint accepts a `"dry_run": true` flag.
It is **the same endpoint, path, auth, request body, and validation** as a normal
send -- a test hits `/api/transactional/send` exactly as production does, adding
only this one flag -- so the whole production send path is exercised. With
`dry_run` set, Datamailer runs the identical validate/render pipeline but stops
before delivery: it **sends nothing, queues nothing, and persists nothing**, so
there are no message rows to poll and no teardown to run.

The response is a superset of the real send response. Alongside the usual
`message`, `idempotent_replay`, and `enqueued` fields it adds the rendered
`subject`/`html_body`/`text_body` plus a `would_deliver` flag and a
`delivery_decision` explaining whether the real send would have been allowed. The
returned `message.id` and `message.created_at` are always `null` because nothing
is stored, and `enqueued` is always `false`.

```json
{
  "message": {
    "id": null,
    "email": "learner@example.com",
    "from_email": "courses",
    "from_email_address": "DataTalks.Club Courses <courses@dtcdev.click>",
    "reply_to": "",
    "cc": [],
    "bcc": [],
    "status": "queued",
    "template_key": "registration-welcome",
    "idempotency_key": "registration-user-123",
    "created_at": null
  },
  "idempotent_replay": false,
  "enqueued": false,
  "rendered": {
    "subject": "Welcome to ML Zoomcamp",
    "html_body": "<p>Thanks ...</p>",
    "text_body": "Thanks ..."
  },
  "would_deliver": true,
  "delivery_decision": {"allowed": true, "reason": ""}
}
```

`status` is `queued` when the real send would deliver and `skipped` when it would
be suppressed; in the suppressed case `would_deliver` is `false` and
`delivery_decision.reason` names the reason.

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

When configured, Datamailer queues hard-bounce, complaint, public unsubscribe,
resubscribe, and transactional skipped/failed events for CMP after the local
Datamailer transaction commits. A background dispatcher posts queued callbacks
and retries failures with backoff.

```text
CMP_WEBHOOK_URL=https://courses.example.com/api/datamailer/events
CMP_WEBHOOK_TOKEN=shared-secret
```

These global settings are the fallback. A Datamailer client can also configure
its own CMP webhook URL and token from the operator client form.

Datamailer sends:

```text
Authorization: Bearer <CMP_WEBHOOK_TOKEN>
```

Implemented callback event types:

```text
contact.hard_bounced
contact.complained
subscription.unsubscribed
subscription.resubscribed
transactional.skipped
transactional.failed
```

Callbacks are stored in the `cmp_callbacks` outbox. Run the dispatcher with:

```bash
python manage.py process_cmp_callbacks --batch-size 25
```

Sandbox deploys install this dispatcher as `datamailer-cmp-callbacks-worker`.
Operators can inspect recent callback status, attempt counts, next retry time,
delivery time, and the last error from the client detail page and Django admin.

## Mailchimp Sync

Datamailer can push contacts into a client's Mailchimp audience with a tag when
they join a recipient-list tree node. This is a one-way sync (Datamailer →
Mailchimp); Datamailer never reads Mailchimp state back.

Each client configures its own Mailchimp credentials, so different clients sync
into different Mailchimp accounts. Configuration is **write-only**: the key can
be set but is never returned.

### Configure credentials (set-only)

```text
PUT /api/client/mailchimp
```

```json
{
  "api_key": "abc123def456xxxxxxxxxxxxxxxxxxxx-us21",
  "list_id": "1a2b3c4d5e",
  "enabled": true
}
```

- `api_key` — Mailchimp API key **including** the `-<datacenter>` suffix (e.g.
  `-us21`). The datacenter is derived from the key; there is no separate field.
- `list_id` — the Mailchimp audience (list) ID to sync tagged contacts into.
- `enabled` — turn sync on/off without resending the key.

Only the fields present in the body are updated. The response is a non-secret
status; the stored key is never echoed back:

```json
{
  "client": "dtc-courses",
  "enabled": true,
  "configured": true,
  "list_id": "1a2b3c4d5e",
  "datacenter": "us21",
  "api_key_set": true
}
```

There is no `GET` for this endpoint. Operators can also set the key, audience ID,
and enabled flag on the client edit form in the product UI.

### Map audience subtrees to tags (set-only)

Each recipient-list tree node can carry a Mailchimp tag. When a contact becomes
an active member of that node — including every ancestor node the cascade
creates, up to the audience root `<all>` — the mapped tag is applied in
Mailchimp. So registering for `ai-dev-tools-zoomcamp-2026:@registered` applies
that node's tag, and a mapping on `<all>` applies an audience-wide tag to anyone
added to any list.

```text
PUT /api/client/mailchimp/tag-mappings
```

```json
{
  "audience": "dtc-courses",
  "mappings": [
    {"list_key": "ai-dev-tools-zoomcamp-2026:@registered", "tag": "ai-dev-tools-zoomcamp-2026"},
    {"list_key": "<all>", "tag": "dtc-courses-audience", "enabled": true}
  ]
}
```

The request is a full reconcile for that audience: mappings not present in the
payload are removed. The response returns the resulting mapping set. The same
mappings can be edited per audience from the client detail page.

### Delivery

Syncs are queued in the `mailchimp_syncs` outbox after the triggering
transaction commits, then dispatched with exponential backoff (Mailchimp
`PUT /lists/{id}/members/{hash}` upsert followed by
`POST .../members/{hash}/tags`). `4xx` responses other than `429` are treated as
permanent failures; `429`, `5xx`, and transport errors retry.

```bash
python manage.py process_mailchimp_syncs --batch-size 25
```

Operators can inspect recent sync status, attempt counts, next retry time, and
the last error from the client detail page and Django admin.
