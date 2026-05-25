# Worker Queue Contracts

Datamailer uses SQS as the durable boundary between Django and Lambda. All queue messages are envelopes with a `contract` name and integer `version`. Version `1` is the only supported contract version.

Workers must treat SQS delivery as at-least-once. A duplicate message must either no-op or converge on the same database state. Payloads carry stable identifiers and routing context only; workers must load mutable state from Postgres before sending, skipping, updating status, or appending events.

## Shared Lambda Behavior

All worker handlers accept AWS SQS-shaped Lambda events:

```json
{
  "Records": [
    {
      "messageId": "message-1",
      "body": "{\"contract\":\"transactional-email\",\"version\":1}"
    }
  ]
}
```

Handlers return the Lambda partial batch response shape:

```json
{
  "batchItemFailures": [
    {"itemIdentifier": "message-id-to-retry"}
  ]
}
```

Malformed JSON, missing required fields, unknown contract names, and unsupported versions fail only the affected SQS record. Valid records in the same batch are acknowledged by omitting their `messageId` from `batchItemFailures`.

## Queue Isolation

- `transactional-email`: high-priority account, verification, password reset, and product emails.
- `campaign-email`: campaign recipient batches.
- `ses-webhooks`: asynchronous SES provider events.
- `email-events`: tracking and event ingest.

Transactional and campaign work use separate queues and workers so campaign backlog cannot delay account-critical email.

## `transactional-email` v1

Purpose: send one pre-created `transactional_messages` row.

Required fields:

| Field | Type | Notes |
|---|---|---|
| `contract` | string | Must be `transactional-email`. |
| `version` | integer | Must be `1`. |
| `transactional_message_id` | positive integer | Source-of-truth row in `transactional_messages`. |
| `client_id` | positive integer | Client scope for authorization and idempotency. |
| `idempotency_key` | string | Stable client/request key. Expected unique with `client_id`. |

Optional fields:

| Field | Type | Notes |
|---|---|---|
| `contact_id` | positive integer | Routing hint; worker still loads the message row. |
| `template_id` | positive integer | Routing hint for the associated template. |
| `template_key` | string | Diagnostic hint for logs. |
| `metadata` | object | Trace IDs or non-authoritative diagnostics. |

Example:

```json
{
  "contract": "transactional-email",
  "version": 1,
  "transactional_message_id": 101,
  "client_id": 7,
  "contact_id": 22,
  "template_id": 4,
  "template_key": "password-reset",
  "idempotency_key": "client-7:password-reset:req-123",
  "metadata": {"trace_id": "trace-1"}
}
```

Idempotency: the future sender must load `transactional_messages`, check `(client_id, idempotency_key)`, and acknowledge already sent/skipped terminal states without another SES call.

## `campaign-email` v1

Purpose: send a bounded batch of previously snapshotted `campaign_recipients` rows.

Required fields:

| Field | Type | Notes |
|---|---|---|
| `contract` | string | Must be `campaign-email`. |
| `version` | integer | Must be `1`. |
| `campaign_id` | positive integer | Source campaign. |
| `batch_id` | string | Stable scheduler-generated batch identifier. |
| `campaign_recipient_ids` | non-empty array of positive integers | Rows to load and process. |

Optional fields:

| Field | Type | Notes |
|---|---|---|
| `idempotency_key` | string | Recommended value: same as `batch_id`. |
| `metadata` | object | Trace IDs or scheduler diagnostics. |

Example:

```json
{
  "contract": "campaign-email",
  "version": 1,
  "campaign_id": 55,
  "batch_id": "campaign-55-batch-0001",
  "campaign_recipient_ids": [501, 502],
  "idempotency_key": "campaign-55-batch-0001",
  "metadata": {"source": "scheduler"}
}
```

Idempotency: the future sender must load each `campaign_recipients` row and only send rows still eligible for send. Rows already marked `sent`, `skipped`, `bounced`, `complained`, or another terminal state must be acknowledged without another SES call.

## `ses-webhooks` v1

Purpose: process SES delivery, bounce, complaint, open, click, send, or reject notifications asynchronously.

Terraform-managed sandbox and production deployments route SES configuration-set events to SNS and subscribe that SNS topic directly to the `ses-webhooks` SQS queue. In that default path, the worker receives raw SNS `Notification` envelopes from SQS and normalizes the embedded SES `Message` into this Datamailer contract before processing it.

The HTTP SES/SNS webhook endpoint remains an optional ingress path for deployments that cannot use direct SNS-to-SQS delivery or need an externally reachable, signature-validated webhook URL. That endpoint performs the same normalization and enqueues this contract payload. Both ingress paths converge on the processor below.

Required fields:

| Field | Type | Notes |
|---|---|---|
| `contract` | string | Must be `ses-webhooks`. |
| `version` | integer | Must be `1`. |
| `provider` | string | Must be `ses`. |
| `provider_event_id` | string | Stable SNS/SES notification ID for dedupe. |
| `notification_type` | string | One of `send`, `reject`, `delivery`, `bounce`, `complaint`, `open`, `click`. |
| `received_at` | string | ISO-8601 timestamp recorded by Datamailer ingress. |

Optional fields:

| Field | Type | Notes |
|---|---|---|
| `ses_message_id` | string | SES message ID for correlation. |
| `mail_message_id` | string | Provider mail object ID when distinct from `ses_message_id`. |
| `raw_payload_s3_key` | string | Pointer to archived raw provider payload, if stored. |
| `metadata` | object | Non-authoritative provider details. |

Example:

```json
{
  "contract": "ses-webhooks",
  "version": 1,
  "provider": "ses",
  "provider_event_id": "sns-message-123",
  "notification_type": "bounce",
  "received_at": "2026-05-24T12:00:00Z",
  "ses_message_id": "ses-message-123",
  "metadata": {"mail_timestamp": "2026-05-24T11:59:59Z"}
}
```

Idempotency: the future processor must deduplicate by `provider_event_id` when present and correlate `ses_message_id` to `campaign_recipients` or `transactional_messages` before appending `email_events` or updating summary columns.

## `email-events` v1

Purpose: ingest first-party tracking events such as opens, clicks, and unsubscribes.

Required fields:

| Field | Type | Notes |
|---|---|---|
| `contract` | string | Must be `email-events`. |
| `version` | integer | Must be `1`. |
| `event_id` | string | Edge-generated event identifier. |
| `event_type` | string | One of `open`, `click`, `unsubscribe`. |
| `occurred_at` | string | ISO-8601 event timestamp. |
| `idempotency_key` | string | Stable key for duplicate tracking requests. |

Optional fields:

| Field | Type | Notes |
|---|---|---|
| `campaign_id` | positive integer | Campaign context if known. |
| `campaign_recipient_id` | positive integer | Preferred campaign recipient source row. |
| `transactional_message_id` | positive integer | Transactional source row if applicable. |
| `contact_id` | positive integer | Contact context if already resolved. |
| `client_id` | positive integer | Client context if already resolved. |
| `audience_id` | positive integer | Audience context if already resolved. |
| `tracking_token` | string | Token used to load source-of-truth state. |
| `url` | string | Click target URL for click events. |
| `metadata` | object | Request diagnostics such as user agent or IP hash. |

Example:

```json
{
  "contract": "email-events",
  "version": 1,
  "event_id": "track-open-123",
  "event_type": "open",
  "occurred_at": "2026-05-24T12:00:01Z",
  "idempotency_key": "open:tracking-token-123:2026-05-24T12:00:01Z",
  "campaign_recipient_id": 501,
  "tracking_token": "tracking-token-123",
  "metadata": {"user_agent": "Mozilla/5.0"}
}
```

Idempotency: the future processor may append duplicate raw `email_events` when product policy allows it, but summary fields such as first open, unique click, and unsubscribe state must use `idempotency_key`, `tracking_token`, and row IDs to avoid double-counting unique events.
