# Initial Issue Backlog

These issues seed the GitHub tracker. They are ordered so foundational work lands before sending/tracking features.

## 1. Scaffold Django project and local dev workflow

Labels: `P0`, `backend`, `infra`

Scope:

- Create Django project and `mailing` app.
- Configure env-based settings with SQLite local fallback and Postgres via `DATABASE_URL`.
- Add health check endpoint.
- Add pytest and ruff.
- Add basic CI for lint/tests.
- Keep frontend minimal with Django templates/admin.

Acceptance:

- `make setup`, `make run`, and `make test` work locally.
- CI runs lint and tests.
- `/health/` returns 200.

## 2. Add core audience/contact/subscription data model

Labels: `P0`, `backend`, `subscriptions`

Scope:

- Add models for organizations, audiences, clients, contacts, subscriptions, tags, and contact tags.
- Add email normalization.
- Add admin screens with search/filtering.
- Add service methods for contact upsert and subscription changes.

Acceptance:

- One email can belong to multiple audiences/clients.
- Subscription state can differ by client/audience.
- Tests cover uniqueness, normalization, and suppression state.

## 3. Add authenticated client API and verification lookup

Labels: `P0`, `api`, `subscriptions`

Scope:

- Add client API key authentication.
- Add contact upsert endpoint.
- Add subscribe/unsubscribe endpoints.
- Add contact status endpoint for verified/subscribed/suppressed state.

Acceptance:

- Client apps can ask whether an email is verified.
- Response includes marketing and transactional send eligibility.
- Unauthorized clients cannot access other client/audience data.

## 4. Add SQS queue contracts and Lambda worker skeletons

Labels: `P0`, `workers`, `infra`

Scope:

- Define queue message schemas for transactional sends, campaign batches, SES webhooks, and event ingest.
- Add Lambda handler skeletons.
- Add idempotency conventions.
- Add local tests for representative SQS events.

Acceptance:

- Worker handlers can be tested locally without AWS.
- Message schemas are documented.
- Partial batch failure behavior is defined.

## 5. Add transactional email templates and send API

Labels: `P0`, `transactional`, `email`, `api`

Scope:

- Add email template model.
- Add transactional message model.
- Add transactional send API.
- Add idempotency key support.
- Enqueue transactional jobs to SQS.

Acceptance:

- Client can request registration/password-reset/email-verification email.
- Duplicate idempotency key does not enqueue/send twice.
- Transactional sends respect hard bounce/complaint suppression.

## 6. Implement transactional sender Lambda with SES

Labels: `P0`, `transactional`, `ses`, `workers`

Scope:

- Implement SES send service.
- Implement transactional sender Lambda.
- Store SES message ID.
- Mark message sent/failed.
- Add retry-safe behavior.

Acceptance:

- Successful send marks `transactional_messages.status = sent`.
- Failed transient send is retried.
- Duplicate SQS delivery does not send twice.

## 7. Add campaigns and recipient snapshotting

Labels: `P0`, `campaigns`, `backend`

Scope:

- Add campaign model.
- Add campaign recipient model.
- Add include/exclude tag filters.
- Add recipient snapshot service.
- Add skip reasons for unverified/unsubscribed/suppressed contacts.

Acceptance:

- A campaign creates one row per intended recipient.
- Skipped contacts have explicit skip reasons.
- Snapshot is stable after campaign queueing.

## 8. Implement campaign sender Lambda

Labels: `P0`, `campaigns`, `ses`, `workers`

Scope:

- Enqueue campaign send batches.
- Send campaign recipients through SES.
- Add tracking pixel and unsubscribe URL.
- Rewrite links through click tracking.
- Respect SES rate limits with concurrency/batch settings.

Acceptance:

- Pending recipients become sent or failed.
- SES message IDs are stored.
- Duplicate SQS delivery does not send twice.

## 9. Add open/click tracking and unsubscribe endpoints

Labels: `P0`, `tracking`, `subscriptions`, `frontend`

Scope:

- Add tracking token generation and hashing.
- Add open pixel endpoint.
- Add click redirect endpoint.
- Add unsubscribe/preferences page.
- Append `email_events`.
- Update recipient/message summary fields.

Acceptance:

- Open endpoint returns a transparent GIF.
- Click endpoint records event and redirects.
- Repeated opens/clicks update total counts without inflating unique counts.
- Unsubscribe works without login.

## 10. Add SES webhook processing and suppression

Labels: `P0`, `ses`, `workers`, `ops`

Scope:

- Add SES/SNS webhook endpoint.
- Validate SNS signatures in production.
- Enqueue webhook events.
- Process deliveries, bounces, complaints, opens, and clicks.
- Suppress hard bounces and complaints.

Acceptance:

- SES events correlate by message ID.
- Bounce/complaint events update contact suppression state.
- Campaign and transactional stats update correctly.

## 11. Add operator campaign/contact UI

Labels: `P1`, `frontend`, `campaigns`

Scope:

- Add campaign list/detail views.
- Add campaign stats dashboard.
- Add contact search/detail view.
- Add contact event timeline.
- Add filters for opened/clicked/not opened/bounced/unsubscribed.

Acceptance:

- Operator can inspect delivery and engagement without shell access.
- Contact history shows subscription, send, open, click, unsubscribe, bounce, and complaint events.

## 12. Add production infrastructure and monitoring

Labels: `P0`, `infra`, `ops`

Scope:

- Define deployment for Django web on cheap ARM.
- Define Postgres deployment.
- Define SQS queues and DLQs.
- Define Lambda workers and IAM permissions.
- Add alarms for queue age, DLQs, Lambda errors, SES failures, and DB health.

Acceptance:

- Infrastructure can deploy staging.
- DLQ and stuck-queue alarms exist.
- Transactional queue is isolated from campaign queue.

## 13. Add import tooling for first audience migration

Labels: `P1`, `backend`, `ops`

Scope:

- Add CSV import command.
- Import contacts, tags, verification, and unsubscribe state.
- Add dry-run and validation reports.

Acceptance:

- DataTalksClub list can be imported safely.
- Import is idempotent.
- Invalid rows are reported without aborting the full import.

## 14. Run first production pilot

Labels: `P0`, `ops`, `email`

Scope:

- Verify SES identity and production access.
- Configure sending domain and tracking domain.
- Send internal pilot.
- Send limited real campaign.
- Compare stats and delivery against current provider.

Acceptance:

- First production email is sent through Datamailer.
- Bounce/complaint/unsubscribe paths are verified.
- Operators can inspect campaign results.
