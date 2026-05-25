output "state_bucket_name" {
  description = "S3 bucket for Terraform state."
  value       = aws_s3_bucket.state.bucket
}

output "aws_region" {
  description = "AWS region for the state bucket."
  value       = var.aws_region
}

output "domain_backend_hcl" {
  description = "Backend config for terraform/domain/backend.hcl."
  value       = <<EOT
bucket         = "${aws_s3_bucket.state.bucket}"
key            = "domain/terraform.tfstate"
region         = "${var.aws_region}"
encrypt        = true
use_lockfile   = true
EOT
}

output "datamailer_sandbox_backend_hcl" {
  description = "Backend config for terraform/datamailer-sandbox/backend.hcl."
  value       = <<EOT
bucket         = "${aws_s3_bucket.state.bucket}"
key            = "datamailer-sandbox/terraform.tfstate"
region         = "${var.aws_region}"
encrypt        = true
use_lockfile   = true
EOT
}
