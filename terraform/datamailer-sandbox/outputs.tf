output "aws_region" {
  description = "AWS region used by this sandbox."
  value       = var.aws_region
}

output "ses_configuration_set_name" {
  description = "SES configuration set for sandbox sends."
  value       = aws_ses_configuration_set.sandbox.name
}

output "ses_verified_email_identities" {
  description = "Email identities Terraform requested for SES sandbox verification."
  value       = sort(tolist(var.ses_sandbox_email_identities))
}

output "ses_mailbox_simulator_recipients" {
  description = "SES mailbox simulator recipients for automated sandbox tests; these do not require recipient verification."
  value = {
    success          = "success@simulator.amazonses.com"
    bounce           = "bounce@simulator.amazonses.com"
    complaint        = "complaint@simulator.amazonses.com"
    out_of_office    = "ooto@simulator.amazonses.com"
    suppression_list = "suppressionlist@simulator.amazonses.com"
  }
}

output "ses_events_topic_arn" {
  description = "SNS topic receiving SES events from the configuration set."
  value       = aws_sns_topic.ses_events.arn
}

output "operator_alarm_topic_arn" {
  description = "SNS topic for sandbox alarms."
  value       = aws_sns_topic.operator_alarms.arn
}

output "queue_urls" {
  description = "Datamailer sandbox queue URLs."
  value = {
    for name, queue in aws_sqs_queue.queue : name => queue.url
  }
}

output "queue_arns" {
  description = "Datamailer sandbox queue ARNs."
  value = {
    for name, queue in aws_sqs_queue.queue : name => queue.arn
  }
}

output "dlq_urls" {
  description = "Datamailer sandbox DLQ URLs."
  value = {
    for name, queue in aws_sqs_queue.dlq : name => queue.url
  }
}

output "app_environment" {
  description = "Environment variable values useful for a local Datamailer sandbox run."
  value = {
    AWS_REGION                        = var.aws_region
    AWS_SES_CONFIGURATION_SET         = aws_ses_configuration_set.sandbox.name
    SQS_TRANSACTIONAL_EMAIL_QUEUE_URL = aws_sqs_queue.queue["transactional-email"].url
    SQS_CAMPAIGN_EMAIL_QUEUE_URL      = aws_sqs_queue.queue["campaign-email"].url
    SQS_SES_WEBHOOKS_QUEUE_URL        = aws_sqs_queue.queue["ses-webhooks"].url
    SQS_EMAIL_EVENTS_QUEUE_URL        = aws_sqs_queue.queue["email-events"].url
    TRANSACTIONAL_EMAIL_QUEUE_NAME    = aws_sqs_queue.queue["transactional-email"].name
    CAMPAIGN_EMAIL_QUEUE_NAME         = aws_sqs_queue.queue["campaign-email"].name
    SES_WEBHOOKS_QUEUE_NAME           = aws_sqs_queue.queue["ses-webhooks"].name
    EMAIL_EVENTS_QUEUE_NAME           = aws_sqs_queue.queue["email-events"].name
  }
}

output "runtime_iam_policy_json" {
  description = "Least-privilege IAM policy JSON for a sandbox Datamailer runtime principal."
  value       = data.aws_iam_policy_document.datamailer_sandbox_runtime.json
}

output "inbound_mail" {
  description = "Optional SES inbound mail test mailbox details. Null when inbound_mail_domain is empty."
  value = var.inbound_mail_domain == "" ? null : {
    domain                 = var.inbound_mail_domain
    dns_zone_name          = var.inbound_mail_dns_zone_name
    dns_zone_id            = var.manage_inbound_dns_records ? data.aws_route53_zone.inbound_mail[0].zone_id : null
    recipients             = [for local_part in sort(tolist(var.inbound_mail_recipients)) : "${local_part}@${var.inbound_mail_domain}"]
    bucket                 = aws_s3_bucket.inbound_mail[0].bucket
    s3_prefix              = var.inbound_mail_s3_prefix
    mx_record              = "10 inbound-smtp.${data.aws_region.current.name}.amazonaws.com"
    verification_txt_name  = "_amazonses.${var.inbound_mail_domain}"
    verification_txt_value = aws_ses_domain_identity.inbound_mail[0].verification_token
    dns_managed            = var.manage_inbound_dns_records
  }
}
