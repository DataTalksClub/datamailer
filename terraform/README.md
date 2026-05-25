# Terraform

This directory contains separate Terraform roots:

- `state/`: bootstrap S3 remote state resources with native S3 lockfiles.
- `domain/`: reusable base DNS setup for `dtcdev.click`.
- `datamailer-sandbox/`: Datamailer-specific sandbox resources that use `dtcdev.click`, including `datamailer@dtcdev.click` inbound mail to S3.

Keep state files private. Do not commit `terraform.tfvars`, `backend.hcl`, `*.tfstate`, AWS credentials, or contact details used for domain registration.

## Order

1. Register `dtcdev.click` in Route 53 or another registrar.
2. Apply `terraform/state` with local state.
3. Generate `backend.hcl` files for `domain` and `datamailer-sandbox` from the `terraform/state` outputs.
4. Apply `terraform/domain` to create/manage the hosted zone.
5. Set the registered domain's name servers to the `terraform/domain` output if registration and hosted zone are not linked automatically.
6. Apply `terraform/datamailer-sandbox`.

`dtcdev.click` was checked in Route 53 and was available at the time of setup. `.click` pricing was `$3` registration and `$3` renewal per year.

## 1. State

```bash
cd terraform/state
terraform init
terraform apply
terraform output -raw domain_backend_hcl > ../domain/backend.hcl
terraform output -raw datamailer_sandbox_backend_hcl > ../datamailer-sandbox/backend.hcl
```

The state root itself can remain local because it only manages the bucket/table used by the other roots. If we later need to share state bootstrap ownership too, migrate it manually after the bucket exists.

## 2. Domain

```bash
cd terraform/domain
terraform init -backend-config=backend.hcl
terraform apply
terraform output name_servers
```

If Route 53 registration did not automatically use this hosted zone, copy the output name servers into the registered domain.

## 3. Datamailer Sandbox

```bash
cd terraform/datamailer-sandbox
terraform init -backend-config=backend.hcl
terraform apply
terraform output inbound_mail
```

The default inbound test address is `datamailer@dtcdev.click`. SES receiving stores raw MIME messages in the S3 bucket shown by `terraform output inbound_mail`.
