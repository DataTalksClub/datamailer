# Terraform State Bootstrap

This root creates the shared remote state resources for the Terraform roots in this directory:

- S3 bucket for state files;
- S3 versioning and server-side encryption;
- S3 public access block and bucket-owner enforced ownership;
- S3 native state lockfiles.

Apply this root first with local state:

```bash
cd terraform/state
terraform init
terraform apply
```

Then write backend files for the other roots from the outputs:

```bash
terraform output -raw domain_backend_hcl > ../domain/backend.hcl
terraform output -raw datamailer_sandbox_backend_hcl > ../datamailer-sandbox/backend.hcl
```

Do not commit generated `backend.hcl` files. Commit only `backend.hcl.example`.
