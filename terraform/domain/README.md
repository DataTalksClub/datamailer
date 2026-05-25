# Shared Sandbox Domain

This Terraform root manages the reusable hosted zone for `dtcdev.click`.

It intentionally does not commit domain-registration contact details. Register `dtcdev.click` in Route 53 or another registrar first, then apply this root to create the hosted zone and get the authoritative name servers.

```bash
terraform init
terraform plan
terraform apply
terraform output name_servers
```

If the domain registration is separate from this hosted zone, update the registered domain to use the output name servers.

Downstream project-specific Terraform roots, such as `../datamailer-sandbox`, look up this hosted zone and create their own records.
