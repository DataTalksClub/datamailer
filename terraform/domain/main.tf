locals {
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    Service     = "shared-domain"
  }
}

resource "aws_route53_zone" "main" {
  name = var.domain_name

  comment = "Reusable sandbox/dev hosted zone for DataTalksClub tests."
}
