# Operations And Reliability

Email sending must be reliable because client applications will depend on Datamailer for verification, password reset, course notifications, and campaigns.

## Production Shape

```text
Django web app
  thin UI, admin, API, tracking endpoints

Postgres
  source of truth and audit trail

SQS
  durable send/event queues with DLQs

Lambda
  bursty campaign sender, transactional sender, SES webhook/event processor

SES
  delivery provider
```

## Why Not Redis As The Primary Queue

Redis is useful for cache and fast ephemeral queues, but the primary send queue should be durable and operationally simple. SQS gives native durability, retries, visibility timeouts, dead-letter queues, CloudWatch metrics, and direct Lambda integration.

Redis or Valkey can be introduced later for caching or rate counters, but it should not be required for the MVP send pipeline.

## Why Lambda Workers

Campaign sending is bursty. The sender is idle most of the time, then active during a blast. Lambda fits this pattern better than an always-running worker container.

Use Lambda for:

- Transactional sender.
- Campaign batch sender.
- SES webhook processor.
- Optional async event processor.

Keep Django web thin and cheap on ARM. Django should enqueue work and serve UI/API/tracking endpoints, not run long campaign sends inside HTTP requests.

## Queue Design

Queues:

- `transactional-email`: highest priority.
- `campaign-email`: campaign batch jobs.
- `email-events`: async event processing when tracking writes become too heavy for request path.
- `ses-webhooks`: provider event processing.

Every queue gets:

- Dead-letter queue.
- CloudWatch alarm on DLQ depth.
- CloudWatch alarm on age of oldest message.
- CloudWatch alarm on Lambda errors.
- Visibility timeout greater than max expected processing time.

Use long polling where a process polls SQS directly. For Lambda event sources, AWS manages polling, and SQS request costs still apply. SQS has a monthly free tier and request pricing is low enough that queue cost should be small compared with delivery and database costs.

## Queue Cost Decision

Use separate queues and Lambda workers for operational isolation:

```text
transactional-email -> transactional sender Lambda
campaign-email      -> campaign sender Lambda
ses-webhooks        -> webhook processor Lambda
email-events        -> tracking/event processor Lambda, optional
```

This is acceptable from a cost perspective.

Assumptions:

- 4 active queues.
- DLQs are not polled continuously.
- 31-day month.
- 20-second long polling interval.
- 2 idle Lambda pollers per queue in the low-traffic case.
- SQS Standard queue pricing after free tier is roughly $0.40 per million requests.
- SQS free tier is 1 million requests per month.

Idle polling estimate:

```text
polls per poller per month
= 31 * 24 * 60 * 60 / 20
= 133,920 requests

4 queues * 2 pollers * 133,920
= 1,071,360 receive requests/month

billable after 1M free tier
= 71,360 requests

cost
= 71,360 / 1,000,000 * $0.40
= ~$0.03/month
```

Pessimistic idle estimate with 5 pollers per queue:

```text
4 queues * 5 pollers * 133,920
= 2,678,400 receive requests/month

billable after 1M free tier
= 1,678,400 requests

cost
= 1,678,400 / 1,000,000 * $0.40
= ~$0.67/month
```

Campaign activity is also small relative to SES. At 720k campaign emails/month and 10 messages per SQS batch:

```text
SendMessageBatch ~= 72k requests
ReceiveMessage   ~= 72k requests
DeleteMessage    ~= 72k requests
Total            ~= 216k requests

cost before free tier
= 216,000 / 1,000,000 * $0.40
= ~$0.09/month
```

Conclusion: SQS queue cost is not a meaningful cost driver. Use separate queues to protect transactional email from campaign backlog and to make retries, DLQs, and alarms easier to reason about.

## Idempotency

SQS is at-least-once. Duplicate deliveries are expected and must be harmless.

Rules:

- A campaign recipient row must not be sent twice.
- A transactional message with the same client/idempotency key must not be sent twice.
- Tracking events may be appended more than once, but summary fields must distinguish total and unique counts.
- SES webhook events should be deduplicated when provider event IDs are available.

Campaign send logic:

```text
load campaign_recipient
if status is sent:
  acknowledge job
else:
  send via SES
  store ses_message_id
  set status = sent
  append sent event
```

## Throttling

SES accounts have daily quota and per-second send rate. Lambda can scale faster than SES accepts. Throttle with:

- Lambda reserved concurrency.
- SQS event source maximum concurrency.
- Small batch sizes.
- App-level token bucket if needed.
- Per-client/audience campaign limits.

Transactional and campaign queues must have separate concurrency controls so a newsletter blast does not delay password resets or verification emails.

## Postgres Connections

Lambda concurrency can exhaust Postgres connections if unchecked.

Controls:

- Conservative Lambda concurrency at launch.
- Short database transactions.
- Small send batches.
- RDS Proxy when concurrency grows.
- Separate database user for workers with least privilege where practical.

## Monitoring

Minimum dashboards/alarms:

- SES bounce and complaint rate.
- SES send failures.
- SQS oldest message age per queue.
- SQS DLQ depth per queue.
- Lambda errors and throttles.
- Lambda duration near timeout.
- Postgres CPU, storage, connections, and slow queries.
- Campaign stuck in `sending` too long.
- Transactional email queued too long.

The issue #13 MVP infrastructure skeleton codifies these alarms and the operations dashboard in
[`infra/cloudformation/datamailer-mvp.json`](../infra/cloudformation/datamailer-mvp.json). Deployment notes live in
[`infra-deploy.md`](infra-deploy.md), and step-by-step incident procedures live in
[`runbooks/infra-operations.md`](runbooks/infra-operations.md).

## Recovery

Recovery tools:

- Retry failed campaign recipients.
- Replay DLQ messages after fixing the cause.
- Recompute campaign aggregate counters from recipient/event tables.
- Suppress a contact manually.
- Pause a campaign.
- Resume a campaign from pending/failed recipients.
