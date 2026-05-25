#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REQUIRED_QUEUES = ("transactional-email", "campaign-email", "ses-webhooks", "email-events")
ENV_QUEUE_URLS = {
    "transactional-email": "SQS_TRANSACTIONAL_EMAIL_QUEUE_URL",
    "campaign-email": "SQS_CAMPAIGN_EMAIL_QUEUE_URL",
    "ses-webhooks": "SQS_SES_WEBHOOKS_QUEUE_URL",
    "email-events": "SQS_EMAIL_EVENTS_QUEUE_URL",
}


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str


def pass_(name, detail):
    return CheckResult(name, "PASS", detail)


def warn(name, detail):
    return CheckResult(name, "WARN", detail)


def fail(name, detail):
    return CheckResult(name, "FAIL", detail)


def client_error_code(exc):
    return exc.response.get("Error", {}).get("Code", "Unknown")


def load_terraform_outputs(terraform_dir="", terraform_output_json=""):
    if terraform_output_json:
        raw = Path(terraform_output_json).read_text()
    elif terraform_dir:
        completed = subprocess.run(
            ["terraform", f"-chdir={terraform_dir}", "output", "-json"],
            check=True,
            capture_output=True,
            text=True,
        )
        raw = completed.stdout
    else:
        return {}

    outputs = json.loads(raw)
    return {name: item.get("value") for name, item in outputs.items()}


def config_from_sources(args):
    outputs = load_terraform_outputs(args.terraform_dir, args.terraform_output_json)
    queue_urls = dict(outputs.get("queue_urls") or {})
    dlq_urls = dict(outputs.get("dlq_urls") or {})
    queue_arns = dict(outputs.get("queue_arns") or {})

    for queue_name, env_name in ENV_QUEUE_URLS.items():
        if os.environ.get(env_name):
            queue_urls[queue_name] = os.environ[env_name]

    region = args.region or os.environ.get("AWS_REGION") or outputs.get("aws_region") or "us-east-1"
    configuration_set = (
        args.ses_configuration_set
        or os.environ.get("AWS_SES_CONFIGURATION_SET")
        or outputs.get("ses_configuration_set_name")
        or ""
    )
    identities = list(outputs.get("ses_verified_email_identities") or [])
    if args.ses_identity:
        identities.extend(args.ses_identity)
    if os.environ.get("AWS_SES_IDENTITIES"):
        identities.extend(item.strip() for item in os.environ["AWS_SES_IDENTITIES"].split(",") if item.strip())

    inbound_mail = outputs.get("inbound_mail") or {}
    inbound_bucket = args.inbound_bucket or os.environ.get("INBOUND_MAIL_BUCKET") or inbound_mail.get("bucket") or ""
    inbound_prefix = args.inbound_prefix or os.environ.get("INBOUND_MAIL_PREFIX") or inbound_mail.get("s3_prefix") or ""
    inbound_domain = args.inbound_domain or os.environ.get("INBOUND_MAIL_DOMAIN") or inbound_mail.get("domain") or ""
    topic_arn = (
        args.ses_events_topic_arn or os.environ.get("SES_EVENTS_TOPIC_ARN") or outputs.get("ses_events_topic_arn") or ""
    )

    return {
        "region": region,
        "queue_urls": queue_urls,
        "queue_arns": queue_arns,
        "dlq_urls": dlq_urls,
        "ses_configuration_set": configuration_set,
        "ses_identities": sorted(set(identities)),
        "ses_events_topic_arn": topic_arn,
        "inbound_bucket": inbound_bucket,
        "inbound_prefix": inbound_prefix,
        "inbound_domain": inbound_domain,
    }


def check_aws_context(sts, region):
    try:
        caller = sts.get_caller_identity()
    except ClientError as exc:
        return [fail("AWS caller", f"{client_error_code(exc)}: {exc}")]
    account = caller.get("Account", "unknown")
    arn = caller.get("Arn", "unknown")
    return [pass_("AWS caller", f"account={account} region={region} arn={arn}")]


def check_queue_exists(sqs, queue_name, queue_url):
    try:
        response = sqs.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=[
                "QueueArn",
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "MessageRetentionPeriod",
            ],
        )
    except ClientError as exc:
        return fail(f"SQS queue {queue_name}", f"{client_error_code(exc)} for {queue_url}")

    attributes = response.get("Attributes", {})
    visible = attributes.get("ApproximateNumberOfMessages", "unknown")
    inflight = attributes.get("ApproximateNumberOfMessagesNotVisible", "unknown")
    retention = attributes.get("MessageRetentionPeriod", "unknown")
    return pass_(
        f"SQS queue {queue_name}",
        f"arn={attributes.get('QueueArn', 'unknown')} visible={visible} inflight={inflight} retention_seconds={retention}",
    )


def smoke_message_body(queue_name, smoke_id):
    return json.dumps(
        {
            "schema_version": 1,
            "message_type": "sandbox_smoke_test",
            "queue": queue_name,
            "smoke_id": smoke_id,
            "dry_run": True,
            "note": "Non-production validation payload. Delete after receipt.",
        },
        sort_keys=True,
    )


def check_queue_round_trip(sqs, queue_name, queue_url):
    smoke_id = f"sandbox-smoke-{int(time.time())}-{uuid.uuid4()}"
    body = smoke_message_body(queue_name, smoke_id)
    try:
        send = sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=body,
            MessageAttributes={"SmokeTestId": {"DataType": "String", "StringValue": smoke_id}},
        )
        for _attempt in range(3):
            response = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=10,
                WaitTimeSeconds=2,
                VisibilityTimeout=5,
                MessageAttributeNames=["All"],
            )
            for message in response.get("Messages", []):
                if message.get("Body") == body:
                    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
                    return pass_(
                        f"SQS round trip {queue_name}",
                        f"sent={send.get('MessageId', 'unknown')} received={message.get('MessageId', 'unknown')} deleted=true",
                    )
    except ClientError as exc:
        return fail(f"SQS round trip {queue_name}", f"{client_error_code(exc)} for {queue_url}")

    return fail(f"SQS round trip {queue_name}", "sent smoke message but did not receive it back for cleanup")


def check_dlq(sqs, queue_name, queue_url):
    try:
        response = sqs.get_queue_attributes(
            QueueUrl=queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "MessageRetentionPeriod",
            ],
        )
    except ClientError as exc:
        return fail(f"SQS DLQ {queue_name}", f"{client_error_code(exc)} for {queue_url}")

    attributes = response.get("Attributes", {})
    visible = int(attributes.get("ApproximateNumberOfMessages", "0"))
    inflight = int(attributes.get("ApproximateNumberOfMessagesNotVisible", "0"))
    retention = attributes.get("MessageRetentionPeriod", "unknown")
    detail = f"visible={visible} inflight={inflight} retention_seconds={retention}"
    if visible or inflight:
        return warn(f"SQS DLQ {queue_name}", detail)
    return pass_(f"SQS DLQ {queue_name}", detail)


def check_ses_configuration_set(sesv2, configuration_set):
    if not configuration_set:
        return [fail("SES configuration set", "missing configuration set name")]
    try:
        sesv2.get_configuration_set(ConfigurationSetName=configuration_set)
    except ClientError as exc:
        return [fail("SES configuration set", f"{client_error_code(exc)} for {configuration_set}")]
    return [pass_("SES configuration set", configuration_set)]


def check_ses_identities(sesv2, identities):
    if not identities:
        return [warn("SES identities", "no identities configured")]
    results = []
    for identity in identities:
        try:
            response = sesv2.get_email_identity(EmailIdentity=identity)
        except ClientError as exc:
            results.append(fail(f"SES identity {identity}", f"{client_error_code(exc)} while reading identity"))
            continue
        status = response.get("VerificationStatus", "NOT_STARTED")
        verified = response.get("VerifiedForSendingStatus", False)
        detail = f"verification_status={status} verified_for_sending={verified}"
        if verified or status == "SUCCESS":
            results.append(pass_(f"SES identity {identity}", detail))
        else:
            results.append(warn(f"SES identity {identity}", f"{detail}; mailbox/domain action may be pending"))
    return results


def check_sns_topic(sns, topic_arn, ses_webhooks_queue_arn=""):
    if not topic_arn:
        return [fail("SNS SES events topic", "missing topic ARN")]
    try:
        sns.get_topic_attributes(TopicArn=topic_arn)
        subscriptions = sns.list_subscriptions_by_topic(TopicArn=topic_arn).get("Subscriptions", [])
    except ClientError as exc:
        return [fail("SNS SES events topic", f"{client_error_code(exc)} for {topic_arn}")]

    results = [pass_("SNS SES events topic", topic_arn)]
    endpoints = [subscription.get("Endpoint", "") for subscription in subscriptions]
    if ses_webhooks_queue_arn and ses_webhooks_queue_arn in endpoints:
        results.append(pass_("SNS to ses-webhooks", f"subscription endpoint={ses_webhooks_queue_arn}"))
    elif endpoints:
        results.append(
            warn("SNS to ses-webhooks", f"expected={ses_webhooks_queue_arn or 'unknown'} available={endpoints}")
        )
    else:
        results.append(fail("SNS to ses-webhooks", "no subscriptions found"))
    return results


def check_inbound_s3(s3, bucket, prefix, require_read):
    if not bucket:
        return [warn("Inbound S3 bucket", "not configured; inbound mail may be disabled")]
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        return [fail("Inbound S3 bucket", f"{client_error_code(exc)} for {bucket}")]

    results = [pass_("Inbound S3 bucket", bucket)]
    try:
        s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    except ClientError as exc:
        result = fail if require_read else warn
        results.append(result("Inbound S3 list", f"{client_error_code(exc)} for s3://{bucket}/{prefix}"))
    else:
        results.append(pass_("Inbound S3 list", f"s3://{bucket}/{prefix}"))
    return results


def check_route53(route53, domain, hosted_zone_id=""):
    if not domain and not hosted_zone_id:
        return [warn("Route53 hosted zone", "not configured; domain delegation is a manual gate")]
    try:
        if hosted_zone_id:
            zone = route53.get_hosted_zone(Id=hosted_zone_id)
        else:
            response = route53.list_hosted_zones_by_name(DNSName=domain, MaxItems="1")
            zones = response.get("HostedZones", [])
            zone = (
                {"HostedZone": zones[0]}
                if zones and zones[0].get("Name", "").rstrip(".") == domain.rstrip(".")
                else None
            )
            if not zone:
                return [warn("Route53 hosted zone", f"no hosted zone found for {domain}")]
            zone = route53.get_hosted_zone(Id=zone["HostedZone"]["Id"])
    except ClientError as exc:
        return [warn("Route53 hosted zone", f"{client_error_code(exc)} while checking {hosted_zone_id or domain}")]

    hosted_zone = zone.get("HostedZone", {})
    name_servers = zone.get("DelegationSet", {}).get("NameServers", [])
    return [
        pass_(
            "Route53 hosted zone",
            f"id={hosted_zone.get('Id', 'unknown')} name={hosted_zone.get('Name', domain)} name_servers={name_servers}",
        )
    ]


def run_checks(config, session, args):
    sqs = session.client("sqs", region_name=config["region"])
    sesv2 = session.client("sesv2", region_name=config["region"])
    sns = session.client("sns", region_name=config["region"])
    s3 = session.client("s3", region_name=config["region"])
    sts = session.client("sts", region_name=config["region"])
    route53 = session.client("route53")

    results = []
    results.extend(check_aws_context(sts, config["region"]))

    for queue_name in REQUIRED_QUEUES:
        queue_url = config["queue_urls"].get(queue_name, "")
        if queue_url:
            results.append(check_queue_exists(sqs, queue_name, queue_url))
        else:
            results.append(fail(f"SQS queue {queue_name}", "missing queue URL"))

    round_trip_queues = REQUIRED_QUEUES if args.round_trip_all_queues else ("transactional-email",)
    for queue_name in round_trip_queues:
        queue_url = config["queue_urls"].get(queue_name, "")
        if queue_url:
            results.append(check_queue_round_trip(sqs, queue_name, queue_url))

    for queue_name in REQUIRED_QUEUES:
        dlq_url = config["dlq_urls"].get(queue_name, "")
        if dlq_url:
            results.append(check_dlq(sqs, queue_name, dlq_url))
        else:
            results.append(
                warn(f"SQS DLQ {queue_name}", "missing DLQ URL; set dlq_urls from Terraform output to inspect it")
            )

    results.extend(check_ses_configuration_set(sesv2, config["ses_configuration_set"]))
    results.extend(check_ses_identities(sesv2, config["ses_identities"]))
    results.extend(check_sns_topic(sns, config["ses_events_topic_arn"], config["queue_arns"].get("ses-webhooks", "")))
    results.extend(
        check_inbound_s3(s3, config["inbound_bucket"], config["inbound_prefix"], args.require_inbound_s3_read)
    )
    results.extend(check_route53(route53, config["inbound_domain"], args.route53_hosted_zone_id))
    results.append(warn("SES production access", "manual gate; not required for this sandbox smoke check"))
    results.append(
        warn(
            "Domain registration/delegation",
            "manual gate; DNS verification and MX propagation do not block this command",
        )
    )
    return results


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run AWS sandbox smoke checks for already-applied Terraform resources."
    )
    parser.add_argument("--terraform-dir", default="", help="Terraform root to read with `terraform output -json`.")
    parser.add_argument("--terraform-output-json", default="", help="Path to a saved `terraform output -json` file.")
    parser.add_argument(
        "--region", default="", help="AWS region. Defaults to AWS_REGION, Terraform output, then us-east-1."
    )
    parser.add_argument("--ses-configuration-set", default="", help="SES configuration set name.")
    parser.add_argument("--ses-identity", action="append", default=[], help="SES identity to report; may be repeated.")
    parser.add_argument("--ses-events-topic-arn", default="", help="SNS topic ARN receiving SES events.")
    parser.add_argument("--inbound-bucket", default="", help="Inbound mail S3 bucket.")
    parser.add_argument("--inbound-prefix", default="", help="Inbound mail S3 prefix.")
    parser.add_argument("--inbound-domain", default="", help="Inbound mail domain for optional Route53 reporting.")
    parser.add_argument("--route53-hosted-zone-id", default="", help="Optional Route53 hosted zone ID.")
    parser.add_argument(
        "--require-inbound-s3-read", action="store_true", help="Treat S3 list permission denial as FAIL."
    )
    parser.add_argument(
        "--round-trip-all-queues", action="store_true", help="Send/delete smoke messages on all active queues."
    )
    return parser


def main(argv=None, session=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    session = session or boto3.Session(region_name=args.region or os.environ.get("AWS_REGION") or None)
    config = config_from_sources(args)
    results = run_checks(config, session, args)

    failed = False
    for result in results:
        print(f"{result.status}: {result.name}: {result.detail}")
        failed = failed or result.status == "FAIL"
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL: sandbox smoke test crashed: {exc}", file=sys.stderr)
        raise
