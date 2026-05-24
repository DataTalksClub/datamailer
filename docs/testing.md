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
- Skipped LocalStack tests when no local AWS endpoint is running.

## Local AWS Tests

Run LocalStack:

```bash
make localstack
```

In another terminal:

```bash
AWS_ACCESS_KEY_ID=test \
AWS_SECRET_ACCESS_KEY=test \
AWS_ENDPOINT_URL=http://localhost:4566 \
make test-aws-local
```

Local AWS tests are marked with:

```python
pytestmark = pytest.mark.aws_local
```

These tests use LocalStack for SQS wiring. They create unique queue names per test and do not depend on real AWS resources.

## What Uses Mocks

SES correctness should primarily use `botocore.stub.Stubber`. LocalStack SES support can be useful for smoke tests, but the important checks are exact payloads and how the app handles returned message IDs.

Lambda workers should expose pure Python handlers that accept AWS event dictionaries. Tests invoke those handlers directly with SQS-shaped events instead of trying to run Lambda inside LocalStack.

## AWS Safety Rules

- Test settings use fake credentials.
- Tests that call AWS-compatible endpoints must pass `endpoint_url`.
- Real AWS credentials are reserved for staging smoke tests.
- Standard SQS is at-least-once, so worker tests must cover idempotency as sender logic is added.
