# Milestones And Tasks

This plan starts with a Django/Postgres/SES service, SQS for durable queues, and Lambda for bursty send/event workers.

## Milestone 0: Project Foundation

Goal: Create a runnable Django project with basic development workflow.

Tasks:

- Scaffold Django project and `mailing` app.
- Configure env-based settings.
- Add Postgres support via `DATABASE_URL`.
- Add local SQLite fallback for development.
- Add AWS/SQS/Lambda configuration placeholders.
- Add pytest/ruff setup.
- Add health check endpoint.
- Add base templates and minimal product navigation.
- Create GitHub repo under `DataTalksClub/datamailer`.

Acceptance:

- `make setup`, `make run`, and `make test` work locally.
- Project has CI for lint and tests.

## Milestone 1: Core Domain Model

Goal: Model organizations, audiences, clients, contacts, subscriptions, and tags.

Tasks:

- Add `Organization`, `Audience`, `Client`, `Contact`, `Subscription`, `Tag`, and `ContactTag` models.
- Add admin screens with useful filters/search.
- Add email normalization.
- Add API key hashing for clients.
- Add service functions for contact upsert and subscription state changes.
- Add tests for normalization, uniqueness, and subscription scoping.

Acceptance:

- One email can belong to multiple audiences/clients with different subscription states.
- Contact status can answer verified/subscribed/suppressed questions.

## Milestone 2: Client API

Goal: Let external apps use Datamailer as the source of truth.

Tasks:

- Add API authentication via client bearer token.
- Add contact upsert endpoint.
- Add subscribe/unsubscribe endpoints.
- Add verification/status lookup endpoint.
- Add structured API error responses.
- Add request logging/audit metadata.
- Add tests for authorization and per-client access boundaries.

Acceptance:

- DataTalksClub courses can ask whether an email is verified/subscribed.
- Client apps can sync contacts and tags without direct database access.

## Milestone 3: Transactional Email

Goal: Send and track product-triggered email.

Tasks:

- Add `EmailTemplate` and `TransactionalMessage` models.
- Add transactional send API.
- Add template rendering with context.
- Add idempotency key support.
- Add SQS job enqueueing for transactional sends.
- Add Lambda transactional sender.
- Add suppression rules for hard bounces and complaints.
- Add tests for registration/password-reset/email-verification-style sends.

Acceptance:

- A client can request a transactional email.
- Datamailer records queued/sent/failed state and SES message ID.

## Milestone 4: Campaigns And Recipient Snapshot

Goal: Create campaigns and snapshot recipients before sending.

Tasks:

- Add `Campaign` and `CampaignRecipient` models.
- Add campaign admin UI.
- Add include/exclude tag filters.
- Add recipient snapshot service.
- Add skip reason handling.
- Add campaign queue action.
- Add tests for recipient selection and skip reasons.

Acceptance:

- A 120k-recipient campaign creates one recipient row per intended contact.
- Skipped recipients have explicit reasons.

## Milestone 5: SES Campaign Sending

Goal: Send campaigns safely through SES.

Tasks:

- Add SES send service.
- Add tracking pixel injection.
- Add link rewriting for click tracking.
- Add unsubscribe/preference links.
- Add SQS campaign batch jobs.
- Add Lambda campaign sender.
- Add rate limiting based on configured SES quota.
- Add retry handling for transient errors.
- Add tests with mocked SES client.

Acceptance:

- Campaign recipients move from `pending` to `sent` or `failed`.
- SES message IDs are stored for correlation.

## Milestone 6: Tracking And Unsubscribe

Goal: Track engagement and handle unsubscribe flows.

Tasks:

- Add open pixel endpoint.
- Add click redirect endpoint.
- Add public unsubscribe/preferences page.
- Add token generation and hashing.
- Add `EmailEvent` append-only model.
- Update recipient/message summary fields on events.
- Add aggregate campaign counters.
- Add tests for unique opens/clicks and repeated events.

Acceptance:

- Open rate, click rate, click-to-open rate, unsubscribe rate, and bounce rate are available.
- Contact history shows tracking and unsubscribe events.

## Milestone 7: SES Webhooks And Suppression

Goal: Capture provider delivery, bounce, and complaint events.

Tasks:

- Add SES/SNS webhook endpoint.
- Validate SNS signatures in production.
- Process delivery events.
- Process bounce events.
- Process complaint events.
- Suppress hard-bounced and complained contacts.
- Correlate events by SES message ID.
- Add webhook tests with representative payloads.

Acceptance:

- Bounced/complained contacts are suppressed.
- Campaign and transactional stats reflect provider events.

## Milestone 8: Product UI And Reporting

Goal: Make the service usable without shell access.

Tasks:

- Add campaign list/detail views.
- Add campaign stats dashboard.
- Add contact search/detail view.
- Add contact event timeline.
- Add tag/audience/client management views where admin is insufficient.
- Add CSV export for campaign recipients and engagement.
- Add filters for opened/clicked/not opened/bounced/unsubscribed.

Acceptance:

- Staff users can manage audiences and inspect delivery outcomes from the UI.

## Milestone 9: Production Hardening

Goal: Prepare for production traffic and growth.

Tasks:

- Add database indexes from the data model doc.
- Add SQS/Lambda monitoring.
- Add structured logging.
- Add error reporting.
- Add rate limits on public tracking and API endpoints.
- Add backups and restore documentation.
- Add migration plan for imports from existing tools.
- Add event retention policy.
- Add monthly partitioning plan for `email_events`.

Acceptance:

- Service can safely handle current 120k-list sends and has a clear growth path.

## Milestone 10: Imports And First Client Migration

Goal: Move the first real audience/client into Datamailer.

Tasks:

- Build import command for CSV/contact exports.
- Import DataTalksClub audience.
- Import tags and subscription state.
- Verify SES identity and production sending access.
- Send seed/test campaigns to internal list.
- Migrate DataTalksClub newsletter or courses as first client.
- Compare delivery and stats with current platform.

Acceptance:

- First production client sends through Datamailer.
- Monthly mailing platform cost can be reduced or removed.
