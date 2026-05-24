# Architecture

Datamailer is a standalone email service used by multiple client applications. It owns audience state, subscription preferences, campaign sending, transactional sending, tracking, and email engagement history.

## Goals

- Replace high per-contact mailing platform costs with SES-based delivery.
- Serve multiple clients: DataTalksClub newsletter, DataTalksClub courses, AI Shipping Labs, and future apps.
- Support shared and separate audiences.
- Provide a UI for operators and an API for client applications.
- Track sends, skips, opens, clicks, unsubscribes, bounces, complaints, and transactional email history.
- Keep an audit trail for every intended campaign recipient.

## Non-Goals For MVP

- Full marketing automation journeys.
- Drag-and-drop email builder.
- Advanced A/B testing.
- Multi-region active-active delivery.
- Replacing every client app auth system.

## Main Components

### Django Web App

Responsibilities:

- Operator UI and Django admin.
- Audience, contact, tag, subscription, campaign, and template management.
- Client API for contact sync, verification lookup, subscription changes, campaign creation, and transactional sends.
- Public endpoints for email verification, unsubscribe, open pixels, click redirects, and hosted preference pages.
- SES webhook endpoints for bounces, complaints, deliveries, opens, and clicks if SES event publishing is enabled.

### Postgres

Postgres is the source of truth for relational product data and event history:

- Contacts and email verification.
- Audiences and clients.
- Client/audience subscription state.
- Tags and contact tag membership.
- Campaign definitions.
- Campaign recipient snapshots.
- Transactional messages.
- Email event timeline.
- Aggregate campaign stats.

Postgres is a good fit because the product needs filtering, auditability, contact history, reporting, and admin workflows.

### SQS Queues

SQS is the durable buffer between the Django control plane and bursty background execution.

Queues:

- `transactional-email`: high-priority registration, password reset, verification, and account email.
- `campaign-email`: campaign send batches.
- `email-events`: optional async processing for opens, clicks, unsubscribes, and other tracking events.
- `ses-webhooks`: async processing for SES delivery, bounce, complaint, open, and click notifications.

Versioned queue message schemas are documented in [`worker-contracts.md`](worker-contracts.md).

Each queue should have:

- A dead-letter queue.
- A visibility timeout longer than the expected Lambda execution time.
- A max receive count appropriate for transient retries.
- CloudWatch alarms for age of oldest message, DLQ depth, and Lambda errors.

SQS standard queues are acceptable because jobs are idempotent. Ordering is not required for campaign sends.

### Lambda Workers

Lambda handles slow or high-volume operations outside HTTP requests:

- Expand campaign filters into recipient snapshots.
- Send campaign emails in bounded batches.
- Send transactional emails.
- Retry transient failures.
- Process SES webhook events asynchronously if needed.
- Recompute or backfill aggregate stats.

Lambda is a good fit because sending is bursty: most of the time there is no campaign send in progress, and transactional volume is comparatively small. SQS plus Lambda gives durable retries without paying for an always-running sender.

Reliability requirements:

- All jobs must be idempotent because SQS is at-least-once.
- Campaign send jobs must check recipient state before sending.
- Transactional send jobs must use idempotency keys.
- SES send-rate limits must be enforced with Lambda reserved concurrency, SQS event source max concurrency, batch sizes, and app-level throttling if needed.
- Postgres connection usage must be bounded with conservative Lambda concurrency and RDS Proxy when needed.

### Amazon SES

SES handles actual email delivery. Datamailer owns the decision to send, message construction, tracking URL generation, and post-send state.

SES integration must cover:

- Verified sender identities.
- Dedicated configuration sets for event publishing.
- Bounce and complaint notifications.
- Sending quota and send-rate throttling.
- Message IDs for correlation back to recipient rows.

## Request Flow

### Campaign Send

1. Operator creates a campaign for a client and audience.
2. Operator selects include/exclude tags and other filters.
3. A Lambda job snapshots intended recipients into `campaign_recipients`.
4. Each recipient row is marked `pending`, `skipped`, or later `sent`/`failed`.
5. Campaign send Lambda workers send through SES in batches.
6. Each sent email contains a tracking pixel, rewritten links, and unsubscribe/preference links.
7. Tracking and SES events update recipient summary columns and append immutable `email_events`.
8. Campaign stats are updated from recipient/event data.

### Transactional Send

1. Client app calls Datamailer API with a template key and recipient.
2. Datamailer validates the client, contact, and suppression rules.
3. Django enqueues a transactional send job in SQS.
4. Transactional send Lambda sends the message through SES.
4. Datamailer records a `transactional_messages` row and event history.

Transactional messages may bypass marketing unsubscribes when legally appropriate, but must still respect hard suppressions such as complaints and permanent bounces.

### Verification Lookup

Client apps can ask whether an email is verified/subscribed:

```text
GET /api/v1/contacts/status?email=person@example.com&audience=datatalks-club&client=dtc-courses
```

The response should include global verification, audience/client subscription state, suppression state, and whether the client is allowed to send transactional or marketing email.

## Deployment Direction

MVP:

- Django web app on a small ARM instance/container.
- Postgres.
- SQS queues.
- Lambda workers.
- SES.

Growth path:

- Add RDS Proxy if Lambda DB connection pressure grows.
- Add ECS/Fargate long-running workers only if Lambda limits become painful.
- Partition high-growth event tables.
- Add read replica for reporting dashboards if needed.
- Archive old raw events to S3 while keeping summary stats in Postgres.
