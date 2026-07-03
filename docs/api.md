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

## Mock Inbox (test-only)

The mock inbox lets end-to-end tests verify that an email was "delivered"
without sending real mail. It is gated behind `MOCK_INBOX_ENABLED` (defaults on
when `DEBUG` or under tests; explicitly opt in on shared deployments).

An address is recognised as a **mock address** when either:

- its domain equals `MOCK_INBOX_DOMAIN` (default `mailbox.test`), e.g.
  `anyone@mailbox.test`; or
- its local part is sub-addressed with `MOCK_INBOX_PLUS_TAG` (default `e2e`),
  e.g. `e2e+homework@example.com`.

Transactional sends to a mock address are still persisted as
`TransactionalMessage` rows (rendered subject/body, template key, context,
metadata, idempotency key) but the worker **skips real SES delivery** and marks
them `sent` with a `ses_message_id` of `mock-inbox:{id}`. These endpoints read
and clear those captured rows. All routes use the same client Bearer auth and
are scoped to the authenticated client. When `MOCK_INBOX_ENABLED` is off, every
route returns `404 {"error": {"code": "mock_inbox_disabled"}}`.

### List captured messages

```text
GET /api/mock-inbox/messages?address=e2e+homework@example.com&limit=25
```

Returns recently captured messages for the mock address, newest first. `limit`
is optional (default 25, max 200). Sending a non-mock address returns
`422 {"error": {"code": "validation_error", "fields": {"address": "not_a_mock_address"}}}`.

```json
{
  "address": "e2e+homework@example.com",
  "count": 1,
  "messages": [
    {
      "id": 42,
      "email": "e2e+homework@example.com",
      "from_email": "newsletter@example.com",
      "subject": "Submission received",
      "template_key": "homework-confirmation",
      "status": "sent",
      "idempotency_key": "homework-submission:123",
      "created_at": "2026-06-20T10:00:00Z"
    }
  ]
}
```

### Fetch one captured message (with body and context)

```text
GET /api/mock-inbox/messages/{message_id}
```

Returns the full message including `html_body`, `text_body`, `context`, and
`metadata`. Returns `404` if the id is unknown, not owned by the client, or not
a mock address.

```json
{
  "message": {
    "id": 42,
    "email": "e2e+homework@example.com",
    "subject": "Submission received",
    "template_key": "homework-confirmation",
    "status": "sent",
    "idempotency_key": "homework-submission:123",
    "created_at": "2026-06-20T10:00:00Z",
    "html_body": "<p>Thanks ...</p>",
    "text_body": "Thanks ...",
    "context": {"course_slug": "e2e-smoke-1718880000"},
    "metadata": {"event": "homework_submission"}
  }
}
```

### Clear captured messages (teardown)

```text
DELETE /api/mock-inbox/messages
```

With a JSON body `{"address": "e2e+homework@example.com"}` deletes captured
messages for that mock address. With no body it deletes **all** mock-addressed
messages for the client (real-recipient messages are never touched). Related
`EmailEvent` rows cascade-delete.

```json
{"address": "e2e+homework@example.com", "deleted_count": 1}
```

## Real Inbox (SES inbound, test-only)

The real inbox is the counterpart to the mock inbox: instead of skipping SES, it
proves an email was **actually sent via SES and received in a real mailbox**. The
receiving side is infrastructure (`datamailer-infra`): an SES receipt rule for the
inbound domain writes every raw MIME message to an S3 bucket. Datamailer really sends
to the test address, and this read API parses the received mail back out of S3.

An address is a **real-inbox address** when its domain (ignoring any `+tag`
sub-address) equals `REAL_INBOX_DOMAIN` (default `mailer.dtcdev.click`), e.g.
`e2e+e2e-smoke-1718880000@mailer.dtcdev.click` or
`datamailer+<tag>@mailer.dtcdev.click`. Such addresses always take the **real SES
send path** (they are never short-circuited by the mock inbox), independent of
`REAL_INBOX_ENABLED`.

The read/clear endpoints below are gated by `REAL_INBOX_ENABLED` and require
`REAL_INBOX_S3_BUCKET` (plus `REAL_INBOX_S3_PREFIX`, default `raw/`). All routes use
the same client Bearer auth. When `REAL_INBOX_ENABLED` is off, every route returns
`404 {"error": {"code": "real_inbox_disabled"}}`. When the bucket is not configured,
they return `503 {"error": {"code": "validation_error", "fields": {"config": "real_inbox_s3_bucket_not_configured"}}}`.

> **Eventual consistency:** SES inbound delivery to S3 is asynchronous. In practice a
> message lands within ~5-15 seconds; e2e tests should poll `GET /api/inbox/messages`
> until `count > 0` (or a timeout of ~60s). Received mail is not scoped to a
> Datamailer client — only to the recipient address — so isolate runs with a unique
> `+<tag>`. The `address` query value contains a `+`; **URL-encode it** (`%2B`) so it
> is not decoded to a space.

### List received messages

```text
GET /api/inbox/messages?address=e2e%2Be2e-smoke-1718880000@mailer.dtcdev.click&limit=25
```

Polls the inbound S3 bucket, parses each raw MIME object, and returns the messages
whose `To`/`Cc`/`X-Original-To` includes the address, newest first by S3
`LastModified`. `limit` is optional (default 25, max 200). A non-real address returns
`422 {"error": {"code": "validation_error", "fields": {"address": "not_a_real_inbox_address"}}}`.

```json
{
  "address": "e2e+e2e-smoke-1718880000@mailer.dtcdev.click",
  "count": 1,
  "messages": [
    {
      "s3_key": "raw/s3oudir75a3gb1k2qlht3mianpfvr5h04ltujlo1",
      "message_id": "<...@email.amazonses.com>",
      "from_email": "no-reply@dtcdev.click",
      "to": ["e2e+e2e-smoke-1718880000@mailer.dtcdev.click"],
      "subject": "Submission received",
      "received_at": "2026-06-20T10:04:22Z"
    }
  ]
}
```

### Fetch one received message (with parsed body and headers)

```text
GET /api/inbox/messages/{s3_key}?address=e2e%2B<tag>@mailer.dtcdev.click
```

`{s3_key}` is the `s3_key` from the list response (it contains `/`, matched as a path).
The `address` query param scopes the lookup so one address cannot read another's mail.
Returns the parsed message including `text_body`, `html_body`, `from_email`, `to`,
`subject`, `message_id`, `received_at`, and the SES `spam_verdict`/`virus_verdict`.
Returns `404` when the key is unknown or not addressed to `address`.

```json
{
  "message": {
    "s3_key": "raw/s3oudir75a3gb1k2qlht3mianpfvr5h04ltujlo1",
    "message_id": "<...@email.amazonses.com>",
    "from_email": "no-reply@dtcdev.click",
    "to": ["e2e+e2e-smoke-1718880000@mailer.dtcdev.click"],
    "subject": "Submission received",
    "received_at": "2026-06-20T10:04:22Z",
    "text_body": "Thanks ...",
    "html_body": "<p>Thanks ...</p>",
    "spam_verdict": "PASS",
    "virus_verdict": "PASS"
  }
}
```

### Clear received messages (teardown)

```text
DELETE /api/inbox/messages?address=e2e%2B<tag>@mailer.dtcdev.click
```

Deletes every received S3 object addressed to the real-inbox `address` (required).
Use a unique `+<tag>` per run so teardown only removes that run's mail.

```json
{"address": "e2e+e2e-smoke-1718880000@mailer.dtcdev.click", "deleted_count": 1}
```

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
