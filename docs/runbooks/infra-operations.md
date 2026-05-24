# Infrastructure Runbooks

Use these runbooks for staging and production operations. Commands are examples; replace stack names, regions, and host names with the active environment values.

## Staging Deploy

1. Validate definitions: `make validate-infra`.
2. Deploy/update `datamailer-staging` with the staging parameter file.
3. Run migrations on the web host or release job: `uv run python manage.py migrate`.
4. Restart web: `sudo systemctl restart datamailer caddy`.
5. Run smoke tests with staging values only.
6. HUMAN: confirm no production sender identity or production queue URL is present in the staging environment.

## Production Deploy

1. Confirm staging smoke tests passed.
2. HUMAN: confirm SES production access, DNS verification, alarm subscriber routing, and recent RDS backup status.
3. Deploy/update `datamailer-production`.
4. Run `uv run python manage.py migrate --check`, then `uv run python manage.py migrate`.
5. Restart web and confirm `/health/` returns `{"status": "ok"}`.
6. Watch queue age, DLQ depth, Lambda errors, RDS connections, and SES bounce/complaint metrics for at least 30 minutes after release.

## Rollback

1. If sends are affected, pause campaign sends first.
2. Restore the previous web release artifact and Lambda `LambdaArtifactKey`.
3. Restart `datamailer` and Caddy.
4. Disable newly failing Lambda event source mappings if workers are causing retries.
5. HUMAN: restore Postgres only after confirming the data loss/corruption window and selecting the target recovery point.

## DLQ Triage And Replay

1. Identify the DLQ alarm source.
2. Sample messages with `aws sqs receive-message` without deleting them.
3. Check Lambda logs for the matching message ID or idempotency key.
4. Fix the root cause before replay.
5. Replay by sending the same message body back to the source queue, then delete the DLQ copy.
6. For campaign jobs, verify recipient state first so idempotency prevents duplicate sends.

## Stuck Queue

1. Compare source queue `ApproximateAgeOfOldestMessage`, visible message count, and Lambda errors.
2. If Lambda errors are high, inspect logs and keep concurrency low.
3. If throttles are high, lower event-source maximum concurrency or reserved concurrency.
4. If Postgres connections are high, lower campaign concurrency first so transactional email remains protected.
5. HUMAN: increase SES quota or Lambda concurrency only after confirming SES account limits and DB capacity.

## Lambda Error Burst

1. Open the worker log group and filter by recent errors.
2. Confirm whether failures are deterministic payload validation errors or transient AWS/DB errors.
3. For deterministic bad payloads, let the message move to DLQ and document the offending schema/idempotency key.
4. For transient issues, verify retries recover and DLQ depth remains zero.
5. If errors threaten transactional email, disable campaign event source mapping before transactional.

## SES Throttling Or Failures

1. Check SES sending quota, max send rate, bounce rate, and complaint rate.
2. Pause campaign sends if bounce/complaint alarms fire.
3. Keep transactional concurrency independent and low.
4. Verify the `ses-webhooks` queue drains through the SES webhook Lambda, the related contact suppression/event rows are written, and the `ses-webhooks-dlq` alarm stays clear. Manually suppress affected contacts only if the webhook worker is degraded or DLQ messages require replay.
5. Resume campaigns only after SES reputation and suppression state are understood.

## Postgres Restore Drill

1. HUMAN: choose a staging restore target before practicing on production data.
2. Restore the latest RDS snapshot to a new staging instance.
3. Point a temporary staging web host at the restored instance.
4. Run `uv run python manage.py migrate --check`.
5. Verify admin login, campaign rows, transactional message rows, and `email_events` visibility.
6. Record restore start/end time and any manual steps needed.

## Campaign Pause And Resume

1. Pause the campaign in the operator/admin surface.
2. Disable or reduce `campaign-email` event-source concurrency if queue pressure continues.
3. Investigate failed recipients and DLQ messages.
4. Resume only pending/failed recipients after the root cause is fixed.
5. Confirm transactional queue age stayed below the alarm threshold throughout the campaign incident.

## Smoke Test Checklist

- Web `/health/` returns 200.
- `migrate --check` passes, then migrations are applied if needed.
- Staging queue round trip succeeds with a dry-run payload.
- Worker dry-run invocation writes to the worker-specific CloudWatch log group and does not send email.
- CloudWatch dashboard and alarms are visible.
- HUMAN: no production sender identity, production queue URL, or production DB URL is present in staging.
- HUMAN: alarm notification reaches the expected on-call destination.
