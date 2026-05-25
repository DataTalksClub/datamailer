variable "aws_region" {
  description = "AWS region for Route 53 domain management APIs."
  type        = string
  default     = "us-east-1"
}

variable "domain_name" {
  description = "Reusable sandbox/dev domain."
  type        = string
  default     = "dtcdev.click"
}

variable "project" {
  description = "Project tag for shared domain resources."
  type        = string
  default     = "dtcdev"
}

variable "environment" {
  description = "Environment tag for shared domain resources."
  type        = string
  default     = "sandbox"
}
