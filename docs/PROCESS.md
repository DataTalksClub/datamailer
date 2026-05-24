# Development Process

## Overview

Datamailer uses GitHub Issues to track work. The repo is public, and issues are the source of truth for product decisions, implementation scope, acceptance criteria, and follow-up tasks.

The process is adapted from AI Shipping Labs, but simplified for this project while the team is small.

## Links

- Repo: https://github.com/DataTalksClub/datamailer
- Issues: https://github.com/DataTalksClub/datamailer/issues
- Docs: [`docs/`](../docs/) folder in this repo

## Issue Lifecycle

```text
Raw issue -> Groomed issue -> Implementation -> Tests/review -> Merge -> CI check
```

1. Capture every substantial request as a GitHub issue.
2. Groom the issue with scope, acceptance criteria, dependencies, and test notes.
3. Implement locally with code and tests.
4. Verify the acceptance criteria and run the relevant test suite.
5. Commit with `Closes #N`.
6. Push to `main`.
7. Check CI and fix failures immediately.

## Labels

Workflow:

- `needs grooming`: raw intake, not ready for implementation.
- `blocked`: cannot proceed without another issue or external decision.
- `human`: requires manual verification outside automated tests.

Priority:

- `P0`: required for first usable production release.
- `P1`: important but not on the first critical path.
- `P2`: useful later.

Area:

- `architecture`
- `infra`
- `backend`
- `frontend`
- `api`
- `email`
- `tracking`
- `subscriptions`
- `campaigns`
- `transactional`
- `ses`
- `workers`
- `ops`
- `docs`

## Working Rules

- Keep issues small enough to implement and review independently.
- Do not send email from HTTP request handlers; enqueue durable jobs.
- Treat Postgres as the source of truth.
- Treat SQS as the durable work boundary.
- Treat Lambda workers as at-least-once processors; all worker code must be idempotent.
- Every campaign send must create a `campaign_recipients` row per intended recipient.
- Every meaningful delivery/tracking state change must be recorded in `email_events`.
- Transactional email must not be blocked behind campaign blasts.
- Unsubscribe and suppression logic must be tested carefully.
- Avoid a heavy frontend. Use Django templates/admin first, HTMX only when it materially improves operator workflows.

## Testing Policy

Minimum expected tests by area:

- Models and service logic: Django unit tests.
- API endpoints: Django client tests.
- Tracking endpoints: request/response tests plus idempotency tests.
- SES integration: mocked boto3/SES payload tests.
- SQS/Lambda workers: handler tests with representative queue events.
- AWS-adjacent flows: LocalStack or equivalent local AWS emulation for SQS/SES wiring where mocks are not enough.
- Operator UI: focused template/view tests first; add Playwright only for critical workflows.

Before merging production-impacting email work, verify:

- Duplicate jobs do not duplicate sends.
- Failed jobs retry safely.
- DLQ path is understood.
- Bounce/complaint suppressions work.
- Unsubscribe links work without login.
- Stats distinguish total events from unique opens/clicks.

Real AWS credentials are not required for normal local development or CI. The test environment should use local mocks and LocalStack-style emulation for SQS/SES/Lambda integration tests, with real AWS reserved for staging smoke tests.

## Temporary Files

Use `.tmp/` for temporary files, screenshots, scratch exports, and local previews. Do not commit `.tmp/`.

## Architecture Decisions

Architecture decisions that affect reliability, cost, data model, compliance, or deployment should be written down in `docs/` before implementation when practical.

Current key decisions:

- Django + Postgres for product/admin/control plane.
- SES for delivery.
- SQS for durable queues.
- Lambda for bursty sender/webhook/event workers.
- Thin frontend with Django templates/admin.
