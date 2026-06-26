# Infrastructure And Deploy

This is the issue #13 MVP deployment path. It is intentionally practical without requiring AWS credentials in CI: generated definitions are validated locally, while account-specific IDs, DNS, SES production access, alarm subscribers, and restore drills remain HUMAN checks.

## Shape

- Django web: one cheap ARM EC2 instance (`t4g.nano` for staging, `t4g.micro` for production by default), Caddy HTTPS, Gunicorn/systemd, WhiteNoise static files, CloudWatch agent logs, and SSM Session Manager access.
- Postgres: RDS Postgres in private subnets, encrypted storage, deletion snapshots, automated backups, CloudWatch log export, and app/worker access through security groups only.
- Queues: separate SQS standard queues and DLQs for `transactional-email`, `campaign-email`, `ses-webhooks`, and `email-events`.
- Workers: Lambda Python 3.12 on `arm64`, existing handlers in `mailing.workers.handlers`, one IAM role per worker scoped to its own source queue/DLQ/log group plus required SES/secrets access, conservative concurrency, partial batch failure reporting, SQS event source mappings, and private subnet access to Postgres.
- SES: per-environment configuration set and sender identity parameters. DNS verification and production access are HUMAN checks.
- Monitoring: CloudWatch log groups with per-environment retention, alarms, and dashboard coverage for queue age, DLQ depth, Lambda errors/throttles/duration, SES bounces/complaints, DB CPU/storage/connections, web health, stuck campaigns, and transactional queue latency.

## Sandbox Intermediate Worker

The current low-cost sandbox runs the Django web app on a single EC2 host with SQLite. Until the sandbox moves to shared Postgres/RDS, SQS workers must run on the same host as the web app so they can read the same database file. Deploying Lambda workers before that database move would let Lambda consume SQS messages but not load the corresponding Django rows.

For this intermediate step, the sandbox deploy installs these systemd units on the EC2 host:

```bash
/opt/datamailer/.venv/bin/python manage.py process_sqs_worker transactional --batch-size 10 --wait-time 20
/opt/datamailer/.venv/bin/python manage.py process_sqs_worker campaign --batch-size 10 --wait-time 20
/opt/datamailer/.venv/bin/python manage.py process_sqs_worker ses-webhooks --batch-size 10 --wait-time 20
/opt/datamailer/.venv/bin/python manage.py process_cmp_callbacks --batch-size 25 --idle-sleep 5
```

The commands long-poll their SQS queues, call the same handlers used by the future Lambda workers, delete only successfully processed records, and leave failed records for SQS retry/DLQ behavior. This is intentionally a sandbox bridge, not the final production architecture.

## Files

- `infra/cloudformation/datamailer-mvp.json`: CloudFormation skeleton for staging/production.
- `infra/config/staging.parameters.example.json`: staging parameter template.
- `infra/config/production.parameters.example.json`: production parameter template.
- `infra/config/web.env.example`: web host environment example.
- `scripts/validate_infra.py`: local validation that does not call AWS.
- `scripts/smoke_test_staging.py`: staging smoke test. HTTP health is automated; AWS checks run only when queue URLs/credentials are provided; remaining promotion checks are printed as HUMAN checks.

## Deploy Flow

1. Build and upload a Lambda artifact zip containing the Django project and dependencies to the environment artifact bucket.
2. Bake or choose an ARM64 AMI with Python 3.12, `uv`, Caddy, CloudWatch agent, and a `datamailer.service` systemd unit.
3. Copy `infra/config/staging.parameters.example.json` to a private parameters file and replace every `REPLACE` value with account-specific IDs.
4. Validate locally:

   ```bash
   make validate-infra
   ```

5. Deploy staging:

   ```bash
   aws cloudformation deploy \
     --stack-name datamailer-staging \
     --template-file infra/cloudformation/datamailer-mvp.json \
     --parameter-overrides file://infra/config/staging.parameters.private.json \
     --capabilities CAPABILITY_NAMED_IAM
   ```

6. On the web host, render `/etc/datamailer/environment` from Secrets Manager/SSM values plus stack outputs, then run:

   ```bash
   uv run python manage.py collectstatic --noinput
   uv run python manage.py migrate
   sudo systemctl restart datamailer
   sudo systemctl restart caddy
   ```

7. Smoke test staging:

   ```bash
   uv run python scripts/smoke_test_staging.py \
     --base-url https://staging.datamailer.example.com \
     --stack-name datamailer-staging \
     --transactional-queue-url "$SQS_TRANSACTIONAL_EMAIL_QUEUE_URL"
   ```

8. Promote to production only after the HUMAN checks in the issue and runbook are complete.

## Web Host Contract

The AMI should provide:

- `datamailer.service`: runs Gunicorn against `datamailer.wsgi:application`, reads `/etc/datamailer/environment`, restarts on failure, and logs to journald.
- `caddy.service`: serves `WebDomainName`, terminates HTTPS through Let's Encrypt, reverse-proxies to Gunicorn, and redirects HTTP to HTTPS.
- CloudWatch agent: ships `journalctl -u datamailer`, Caddy access/error logs, and system metrics.
- SSM Session Manager: preferred admin access; SSH key is optional and should be disabled for production when possible.

Static files use WhiteNoise from Django. `collectstatic --noinput` must be part of the release step.

Rollback path:

1. Stop deploys and pause campaign sends if delivery is affected.
2. Repoint `LambdaArtifactKey` and the web checkout/systemd unit to the previous release artifact.
3. Restart Lambda/web services.
4. If a migration caused data issues, follow the Postgres restore drill in [runbooks/infra-operations.md](runbooks/infra-operations.md). Database restores are a HUMAN decision.

## Worker IAM And Logs

Each Lambda worker has a dedicated runtime role:

- `TransactionalEmailWorkerRole`: reads/deletes only `transactional-email`, can write only `transactional-email-dlq`, can send through SES, can read the DB secret, and can write only its own Lambda log group.
- `CampaignEmailWorkerRole`: reads/deletes only `campaign-email`, can write only `campaign-email-dlq`, can send through SES, can read the DB secret, and can write only its own Lambda log group.
- `SesWebhooksWorkerRole`: reads/deletes only `ses-webhooks`, can write only `ses-webhooks-dlq`, can read the DB secret, and has no SES send permissions. Its event-source mapping is active so SES/SNS notifications are processed by the webhook worker.
- `EmailEventsWorkerRole`: reads/deletes only `email-events`, can write only `email-events-dlq`, can read the DB secret, and has no SES send permissions. Its event-source mapping remains intentionally disabled until optional async event processing is enabled.

The template creates `/aws/lambda/${ProjectName}-${EnvironmentName}-<worker>` log groups with `LambdaLogRetentionDays`; examples use 14 days for staging and 30 days for production. Worker roles use inline runtime permissions instead of broad Lambda execution managed policies so log writes stay scoped to the worker log group.

## Postgres

- RDS backups are enabled through `DBBackupRetentionDays`; staging defaults to 7 days, production example to 14 days.
- `DeletionPolicy` and `UpdateReplacePolicy` are `Snapshot`.
- Start with conservative Lambda concurrency: transactional 4, campaign 2, SES webhooks 2, email events 1.
- If `DatabaseConnections` alarms or connection wait errors appear, first lower Lambda event-source maximum concurrency; add RDS Proxy when sustained worker pressure requires it.
- Use separate credentials where practical: `datamailer_app` for web, `datamailer_worker` for Lambda, and an admin/migration user kept out of runtime processes.

## SES Assumptions

Per environment, set:

- Verified identity: `SESSenderIdentity`.
- Sender default: `DefaultFromEmail`.
- Configuration set: `SESConfigurationSetName`.
- Region: `SESRegion`/`AWS_REGION`.
- DNS: DKIM, SPF, DMARC, and optional custom MAIL FROM.
- Event publishing: SES/SNS webhook processing is active through the `ses-webhooks` queue and Lambda event source mapping. Before production sends, smoke-test a bounce/complaint notification path and verify the queue drains, the worker logs the notification, the DLQ stays empty, and alarms route to the on-call channel.

HUMAN checks before production traffic:

- SES sandbox exit and production send quota are approved.
- DNS records validate in SES.
- Bounce and complaint routing is verified end to end in staging before production sends.
- Alarm notification routing reaches the on-call channel.
