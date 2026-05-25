variable "aws_region" {
  description = "AWS region for the sandbox environment."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name used in resource names."
  type        = string
  default     = "sandbox"
}

variable "project" {
  description = "Project name used in resource names."
  type        = string
  default     = "datamailer"
}

variable "ses_sandbox_email_identities" {
  description = "Email identities to verify in SES sandbox. AWS sends a verification email to each address."
  type        = set(string)
  default = [
    "alexey@datatalks.club",
    "alexey.s.grigoriev@gmail.com",
    "alexey@aishippinglabs.com",
  ]
}

variable "queue_retention_seconds" {
  description = "Message retention for sandbox SQS queues."
  type        = number
  default     = 1209600
}

variable "queue_receive_wait_time_seconds" {
  description = "SQS long-poll wait time for active Datamailer queues."
  type        = number
  default     = 20
}

variable "max_receive_count" {
  description = "SQS redrive max receive count before messages move to DLQ."
  type        = number
  default     = 5
}

variable "alarm_email" {
  description = "Optional email address for SNS alarm notifications. Leave empty to skip subscription."
  type        = string
  default     = ""
}

variable "inbound_mail_domain" {
  description = "Domain for SES inbound mail testing. Defaults to the shared sandbox domain."
  type        = string
  default     = "dtcdev.click"
}

variable "manage_inbound_dns_records" {
  description = "Whether Terraform should manage SES verification and MX records in the Route 53 hosted zone for inbound_mail_domain."
  type        = bool
  default     = true
}

variable "inbound_mail_recipients" {
  description = "Local parts accepted by the SES receipt rule when inbound_mail_domain is set."
  type        = set(string)
  default     = ["datamailer"]
}

variable "inbound_mail_s3_prefix" {
  description = "S3 prefix for raw inbound SES messages."
  type        = string
  default     = "raw/"
}
