variable "aws_region" {
  description = "AWS region for Terraform state resources."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project prefix for Terraform state resources."
  type        = string
  default     = "datamailer"
}

variable "environment" {
  description = "Environment tag for Terraform state resources."
  type        = string
  default     = "sandbox"
}

variable "state_bucket_name" {
  description = "Globally unique S3 bucket name for Terraform state. Leave empty to derive one from account/region."
  type        = string
  default     = ""
}
