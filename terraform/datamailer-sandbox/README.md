# Datamailer Terraform Sandbox

This folder prepares a low-cost AWS sandbox environment for Datamailer integration testing. It is intentionally not the production stack. SES production access can be requested separately, but this root still works in SES sandbox mode for verified addresses and simulator recipients.

The sandbox creates:

- Four SQS queues plus DLQs: `transactional-email`, `campaign-email`, `ses-webhooks`, and `email-events`. Active queues use 20-second long polling by default.
- An SNS topic for SES events and an SQS subscription into the `ses-webhooks` queue.
- An SES configuration set that publishes send/delivery/bounce/complaint/reject/rendering-failure events to SNS.
- SES email identities for sandbox testing.
- Basic SQS queue-age and DLQ CloudWatch alarms.
- A least-privilege IAM policy document output for whichever sandbox role/user runs Datamailer.
- SES inbound receiving for `datamailer@mailer.dtcdev.click`, storing raw MIME messages in S3.

SES event delivery in this Terraform root uses direct SNS-to-SQS routing: SES configuration set -> SNS topic -> `ses-webhooks` SQS queue -> Datamailer webhook worker. The worker accepts the raw SNS notification envelope from SQS and normalizes the embedded SES event before updating Datamailer state.

The HTTP SES/SNS webhook endpoint is still available as an optional alternate ingress for deployments that need a public webhook URL, but it is not the default path created by this Terraform root.

## SES Sandbox Emails

By default Terraform requests SES verification for:

- `alexey@datatalks.club`
- `alexey.s.grigoriev@gmail.com`
- `alexey@aishippinglabs.com`

In an SES sandbox account, both sender and recipient addresses must be verified. AWS will send a verification email to each address. Click those links before testing sends.

Terraform can request verification, but it cannot complete it. The mailbox owner must click the AWS verification link. If we need another tester, add that address to `ses_sandbox_email_identities` and apply again.

For automated tests that do not require a real inbox, use the AWS SES mailbox simulator recipients exposed by `terraform output ses_mailbox_simulator_recipients`:

- `success@simulator.amazonses.com`
- `bounce@simulator.amazonses.com`
- `complaint@simulator.amazonses.com`
- `ooto@simulator.amazonses.com`
- `suppressionlist@simulator.amazonses.com`

These simulator recipients are accepted in SES sandbox without recipient verification and let us test delivery, bounce, complaint, out-of-office, and suppression-list handling. They do not give us a readable inbox. Human inbox verification still needs one of the real addresses above.

## SES Production Access

Terraform does not manage SES account production access. That is SES account/region state, not infrastructure state.

For the current sandbox AWS account in `us-east-1`, a production-access request was submitted with:

- mail type: `MARKETING`
- website: `https://datatalks.club`
- contacts: `alexey@datatalks.club`, `alexey.s.grigoriev@gmail.com`, `alexey@aishippinglabs.com`

AWS returned `DENIED` for case `177969086000902`. Until AWS approves a follow-up request, this account remains in SES sandbox mode with a 200 emails/day quota and 1 email/second send rate.

## Usage

Apply `../state` first and write `backend.hcl` from its output. Then initialize this root with remote state:

```bash
terraform init -backend-config=backend.hcl
```

Copy the example variables if you want to customize values:

```bash
cp terraform.tfvars.example terraform.tfvars
```

Review and apply the plan:

```bash
terraform plan
terraform apply
```

Useful outputs:

```bash
terraform output queue_urls
terraform output app_environment
terraform output ses_configuration_set_name
terraform output ses_mailbox_simulator_recipients
terraform output inbound_mail
terraform output -raw runtime_iam_policy_json
```

For a local Datamailer run against sandbox queues/SES, copy values from `terraform output app_environment` into your local environment. Keep using local/staging-safe sender values until the SES identities above are verified.

Attach `runtime_iam_policy_json` to the sandbox IAM principal used by local Datamailer, AI Shipping Labs, or another future client app. Do not create or commit access keys here.

## Smoke Check

After Terraform has been applied and AWS credentials are available for the sandbox account, run:

```bash
make smoke-sandbox
```

This runs `scripts/smoke_test_sandbox.py` against the resources from `terraform output -json`. It checks AWS caller context, SQS queues and DLQs, a safe transactional SQS send/receive/delete round trip, the SES configuration set and identity verification states, SNS wiring to `ses-webhooks`, inbound S3 visibility, and optional Route53 state.

The smoke check prints `PASS`, `WARN`, and `FAIL` lines. It exits non-zero only for `FAIL`. SES production access, unverified SES identities, pending domain registration/delegation, and DNS propagation are warning/manual gates so the command remains useful in SES sandbox mode.

Useful overrides:

```bash
uv run python scripts/smoke_test_sandbox.py --terraform-dir terraform/datamailer-sandbox
uv run python scripts/smoke_test_sandbox.py --terraform-output-json /path/to/terraform-output.json
uv run python scripts/smoke_test_sandbox.py --terraform-dir terraform/datamailer-sandbox --round-trip-all-queues
uv run python scripts/smoke_test_sandbox.py --terraform-dir terraform/datamailer-sandbox --require-inbound-s3-read
```

## Inbound Test Mailbox

The default inbound address is:

```text
datamailer@mailer.dtcdev.click
```

SES receiving stores raw MIME messages in the S3 bucket shown by:

```bash
terraform output inbound_mail
```

Use this for inbox-style tests:

1. Send a Datamailer transactional email to `datamailer@mailer.dtcdev.click`.
2. Wait for SES receiving to write a raw MIME object under the configured `raw/` prefix.
3. Inspect the S3 object to verify headers, body, unsubscribe links, and tracking links.

Fixture-first inspection does not require AWS credentials:

```bash
uv run python scripts/inspect_inbound_mail.py \
  --fixture tests/fixtures/inbound/sample.eml \
  --expect-to datamailer@mailer.dtcdev.click \
  --expect-subject "inbound smoke" \
  --expect-unsubscribe-link \
  --expect-tracking-substring track.example.com
```

After inbound S3 is receiving real mail, inspect the latest object from Terraform output, or fetch an exact key:

```bash
uv run python scripts/inspect_inbound_mail.py --terraform-dir terraform/datamailer-sandbox --latest
uv run python scripts/inspect_inbound_mail.py --terraform-dir terraform/datamailer-sandbox --s3-key raw/example-message
```

## Placement

Keep this Terraform in Datamailer for now. It is intentionally self-contained so it can later be moved to a shared DataTalksClub/AI Shipping Labs infrastructure repository if we decide to centralize email infrastructure.

## What This Does Not Do

- It does not request SES production access. That request is account/region state handled through SESv2 account details.
- It does not configure DKIM/SPF/DMARC for production deliverability. It only configures the SES identity and MX/TXT records needed for inbound test mail when `manage_inbound_dns_records = true`.
- It does not provision the production EC2/RDS/Lambda stack from `infra/cloudformation/datamailer-mvp.json`.
- It does not create the shared `dtcdev.click` hosted zone; apply `../domain` first.

## Cleanup

```bash
terraform destroy
```

Destroying the sandbox deletes queues and any unprocessed messages in them.
