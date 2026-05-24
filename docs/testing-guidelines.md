# Testing Guidelines

Rules for writing and reviewing Datamailer tests.

## Core Principle

Every assertion must fail if the feature is broken. Avoid tests that only prove Django, boto3, or the browser exists.

## Default Commands

```bash
make test
make lint
```

Run setup after dependency or migration changes:

```bash
make setup
```

Run LocalStack-backed tests when AWS-local behavior is in scope:

```bash
make localstack
make test-aws-local
```

## AWS Testing

- No normal test may require real AWS credentials.
- Use fake credentials in local/CI test mode.
- Always pass `endpoint_url` for LocalStack clients.
- Use LocalStack for SQS queue wiring and enqueue/receive flows.
- Use `botocore.stub.Stubber` for deterministic SES payload tests.
- Invoke Lambda handlers directly with SQS-shaped event dictionaries.
- Keep real AWS for staging smoke tests only.

## SQS And Worker Tests

SQS is at-least-once. Tests for worker logic should prove duplicate delivery is harmless once domain models exist.

Cover:

- Valid message succeeds.
- Invalid message appears in Lambda partial batch failure response.
- Duplicate message does not duplicate sends.
- DLQ/redrive configuration is documented or testable locally.
- Transactional queue is isolated from campaign queue.

## SES Tests

Do not send real email in tests.

Use `Stubber` to assert:

- sender address
- recipients
- subject
- HTML/text body
- configuration set when configured
- returned SES message ID is stored or propagated

## Django Tests

- Prefer focused service tests for domain logic.
- Use Django client tests for API/view behavior.
- Test business rules, not Django defaults.
- Do not test field defaults, `CASCADE`, basic `unique=True`, or URL resolution separately unless custom behavior exists.
- For filters, assert both included and excluded rows.
- For state transitions, assert the state changed from a meaningful prior state.

## UI Tests

Start with Django view/template tests. Add Playwright only when the UI workflow is critical and cannot be verified well with Django tests.

For UI-visible changes, save screenshots under `.tmp/` and upload them with the sandbox screenshot CLI:

```bash
cd /home/alexey/git/sandbox-screenshots
upload-screenshot /home/alexey/git/datamailer/.tmp/screenshot.png
```

Read `/home/alexey/git/sandbox-screenshots/README.md` before uploading. Do not use an orphan `screenshots` branch. If a local or remote `screenshots` branch exists, delete it, reupload screenshots with `upload-screenshot`, and update affected issue comments.

For backend-only issues, screenshots are not applicable; tester should state that explicitly.
