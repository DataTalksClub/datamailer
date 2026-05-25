locals {
  name_prefix          = "${var.project}-${var.environment}"
  inbound_mail_enabled = var.inbound_mail_domain != ""

  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    Service     = "datamailer"
  }

  queues = {
    transactional-email = {
      visibility_timeout_seconds = 180
    }
    campaign-email = {
      visibility_timeout_seconds = 900
    }
    ses-webhooks = {
      visibility_timeout_seconds = 180
    }
    email-events = {
      visibility_timeout_seconds = 180
    }
  }
}

data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

data "aws_route53_zone" "inbound_mail" {
  count = local.inbound_mail_enabled && var.manage_inbound_dns_records ? 1 : 0

  name         = var.inbound_mail_dns_zone_name
  private_zone = false
}

resource "aws_sqs_queue" "dlq" {
  for_each = local.queues

  name                      = "${local.name_prefix}-${each.key}-dlq"
  message_retention_seconds = var.queue_retention_seconds
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "queue" {
  for_each = local.queues

  name                       = "${local.name_prefix}-${each.key}"
  message_retention_seconds  = var.queue_retention_seconds
  receive_wait_time_seconds  = var.queue_receive_wait_time_seconds
  visibility_timeout_seconds = each.value.visibility_timeout_seconds
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = var.max_receive_count
  })
}

resource "aws_sns_topic" "ses_events" {
  name = "${local.name_prefix}-ses-events"
}

resource "aws_sns_topic_subscription" "ses_events_to_queue" {
  topic_arn = aws_sns_topic.ses_events.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.queue["ses-webhooks"].arn
}

resource "aws_sqs_queue_policy" "ses_webhooks_from_sns" {
  queue_url = aws_sqs_queue.queue["ses-webhooks"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowSesEventsTopicToSendMessages"
        Effect = "Allow"
        Principal = {
          Service = "sns.amazonaws.com"
        }
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.queue["ses-webhooks"].arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_sns_topic.ses_events.arn
          }
        }
      }
    ]
  })
}

resource "aws_sns_topic" "operator_alarms" {
  name = "${local.name_prefix}-operator-alarms"
}

resource "aws_sns_topic_subscription" "operator_alarm_email" {
  count = var.alarm_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.operator_alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

resource "aws_ses_configuration_set" "sandbox" {
  name = local.name_prefix
}

resource "aws_ses_event_destination" "sandbox_sns" {
  name                   = "${local.name_prefix}-sns-events"
  configuration_set_name = aws_ses_configuration_set.sandbox.name
  enabled                = true

  matching_types = [
    "bounce",
    "complaint",
    "delivery",
    "reject",
    "renderingFailure",
    "send",
  ]

  sns_destination {
    topic_arn = aws_sns_topic.ses_events.arn
  }
}

resource "aws_ses_email_identity" "sandbox_testers" {
  for_each = var.ses_sandbox_email_identities

  email = each.value
}

resource "aws_s3_bucket" "inbound_mail" {
  count = local.inbound_mail_enabled ? 1 : 0

  bucket = "${local.name_prefix}-${data.aws_caller_identity.current.account_id}-inbound-mail"
}

resource "aws_s3_bucket_public_access_block" "inbound_mail" {
  count = local.inbound_mail_enabled ? 1 : 0

  bucket                  = aws_s3_bucket.inbound_mail[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "inbound_mail" {
  count = local.inbound_mail_enabled ? 1 : 0

  bucket = aws_s3_bucket.inbound_mail[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "inbound_mail" {
  count = local.inbound_mail_enabled ? 1 : 0

  bucket = aws_s3_bucket.inbound_mail[0].id

  rule {
    id     = "expire-inbound-mail"
    status = "Enabled"

    filter {
      prefix = var.inbound_mail_s3_prefix
    }

    expiration {
      days = 14
    }
  }
}

resource "aws_ses_domain_identity" "inbound_mail" {
  count = local.inbound_mail_enabled ? 1 : 0

  domain = var.inbound_mail_domain
}

resource "aws_route53_record" "inbound_mail_ses_verification" {
  count = local.inbound_mail_enabled && var.manage_inbound_dns_records ? 1 : 0

  zone_id = data.aws_route53_zone.inbound_mail[0].zone_id
  name    = "_amazonses.${var.inbound_mail_domain}"
  type    = "TXT"
  ttl     = 300
  records = [aws_ses_domain_identity.inbound_mail[0].verification_token]
}

resource "aws_route53_record" "inbound_mail_mx" {
  count = local.inbound_mail_enabled && var.manage_inbound_dns_records ? 1 : 0

  zone_id = data.aws_route53_zone.inbound_mail[0].zone_id
  name    = var.inbound_mail_domain
  type    = "MX"
  ttl     = 300
  records = ["10 inbound-smtp.${data.aws_region.current.name}.amazonaws.com"]
}

resource "aws_ses_receipt_rule_set" "inbound_mail" {
  count = local.inbound_mail_enabled ? 1 : 0

  rule_set_name = "${local.name_prefix}-inbound-mail"
}

resource "aws_s3_bucket_policy" "inbound_mail" {
  count = local.inbound_mail_enabled ? 1 : 0

  bucket = aws_s3_bucket.inbound_mail[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowSesReceiptRuleToWrite"
        Effect = "Allow"
        Principal = {
          Service = "ses.amazonaws.com"
        }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.inbound_mail[0].arn}/${var.inbound_mail_s3_prefix}*"
        Condition = {
          StringEquals = {
            "AWS:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "AWS:SourceArn" = "arn:aws:ses:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:receipt-rule-set/${aws_ses_receipt_rule_set.inbound_mail[0].rule_set_name}:receipt-rule/${local.name_prefix}-inbound-mail-to-s3"
          }
        }
      }
    ]
  })
}

resource "aws_ses_receipt_rule" "inbound_mail_to_s3" {
  count = local.inbound_mail_enabled ? 1 : 0

  name          = "${local.name_prefix}-inbound-mail-to-s3"
  rule_set_name = aws_ses_receipt_rule_set.inbound_mail[0].rule_set_name
  enabled       = true
  scan_enabled  = true
  recipients = [
    for local_part in var.inbound_mail_recipients : "${local_part}@${var.inbound_mail_domain}"
  ]

  s3_action {
    bucket_name       = aws_s3_bucket.inbound_mail[0].bucket
    object_key_prefix = var.inbound_mail_s3_prefix
    position          = 1
  }

  depends_on = [aws_s3_bucket_policy.inbound_mail]
}

resource "aws_ses_active_receipt_rule_set" "inbound_mail" {
  count = local.inbound_mail_enabled ? 1 : 0

  rule_set_name = aws_ses_receipt_rule_set.inbound_mail[0].rule_set_name
}

resource "aws_cloudwatch_metric_alarm" "queue_oldest_message" {
  for_each = aws_sqs_queue.queue

  alarm_name          = "${local.name_prefix}-${each.key}-oldest-message"
  alarm_description   = "Datamailer sandbox ${each.key} queue has stale messages."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  period              = 300
  statistic           = "Maximum"
  threshold           = each.key == "campaign-email" ? 900 : 300
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.operator_alarms.arn]

  dimensions = {
    QueueName = each.value.name
  }
}

resource "aws_cloudwatch_metric_alarm" "dlq_visible_messages" {
  for_each = aws_sqs_queue.dlq

  alarm_name          = "${local.name_prefix}-${each.key}-messages"
  alarm_description   = "Datamailer sandbox ${each.key} DLQ has messages."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.operator_alarms.arn]

  dimensions = {
    QueueName = each.value.name
  }
}

data "aws_iam_policy_document" "datamailer_sandbox_runtime" {
  statement {
    sid = "SqsDatamailerSandboxQueues"
    actions = [
      "sqs:ChangeMessageVisibility",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ReceiveMessage",
      "sqs:SendMessage",
    ]
    resources = concat(
      [for queue in aws_sqs_queue.queue : queue.arn],
      [for queue in aws_sqs_queue.dlq : queue.arn],
    )
  }

  statement {
    sid = "SesDatamailerSandboxSend"
    actions = [
      "ses:SendEmail",
      "ses:SendRawEmail",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "ses:ConfigurationSet"
      values   = [aws_ses_configuration_set.sandbox.name]
    }
  }

  dynamic "statement" {
    for_each = local.inbound_mail_enabled ? [1] : []

    content {
      sid = "S3InboundMailRead"
      actions = [
        "s3:GetObject",
        "s3:ListBucket",
      ]
      resources = [
        aws_s3_bucket.inbound_mail[0].arn,
        "${aws_s3_bucket.inbound_mail[0].arn}/*",
      ]
    }
  }
}
