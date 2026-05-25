output "domain_name" {
  description = "Reusable sandbox/dev domain."
  value       = var.domain_name
}

output "hosted_zone_id" {
  description = "Route 53 hosted zone ID."
  value       = aws_route53_zone.main.zone_id
}

output "name_servers" {
  description = "Name servers to set on the registered domain if needed."
  value       = aws_route53_zone.main.name_servers
}
