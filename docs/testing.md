# Testing

Datamailer tests must not require real AWS credentials for normal local development or CI.

## Default Test Suite

Run:

```bash
make test
```

The default suite uses:

- Django unit/view tests.
- Pure Python worker helper tests.
- `botocore.stub.Stubber` for SES payload assertions.

LocalStack tests are intentionally outside the default collection path in
`tests_integration/`.

## Local AWS Tests

Run:

```bash
make test-aws-local
```

This starts LocalStack through Docker Compose, waits for its health endpoint, and
runs the integration tests against `http://localhost:4566`.

Local AWS tests are marked with:

```python
pytestmark = pytest.mark.aws_local
```

These tests use LocalStack for SQS wiring. They create unique queue names per test and do not depend on real AWS resources.
They live in `tests_integration/` so normal `pytest` runs do not report
environment-dependent skips.

## What Uses Mocks

SES correctness should primarily use `botocore.stub.Stubber`. LocalStack SES support can be useful for smoke tests, but the important checks are exact payloads and how the app handles returned message IDs.

Lambda workers should expose pure Python handlers that accept AWS event dictionaries. Tests invoke those handlers directly with SQS-shaped events instead of trying to run Lambda inside LocalStack.

## AWS Safety Rules

- Test settings use fake credentials.
- Tests that call AWS-compatible endpoints must pass `endpoint_url`.
- Real AWS credentials are reserved for staging smoke tests.
- Standard SQS is at-least-once, so worker tests must cover idempotency as sender logic is added.

## Sandbox SES Event Smoke

Run the SES mailbox simulator event path only against an applied sandbox:

```bash
make smoke-sandbox-ses-events
```

The command preflights the configured SES sender identity before sending. If no verified email sender is available, it prints a warning and skips the live simulator sends without requiring SES production access.

Before running this command locally, make sure the normal Django runtime is ready:

- `.env` exists, for example from `make setup`.
- The local database is reachable.
- Migrations have been applied with `make migrate`.

The smoke creates temporary transactional-message rows in the configured local database, sends three SES mailbox simulator messages, waits for SES to publish the related SNS/SQS events, runs the webhook worker path, and verifies delivery, bounce, and complaint effects in the database. SES can emit multiple notifications for one send, so the command drains SQS until the expected database effects are observed or the timeout expires.
