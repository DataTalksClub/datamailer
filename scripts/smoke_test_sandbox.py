#!/usr/bin/env python3
import argparse
import importlib
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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REQUIRED_QUEUES = ("transactional-email", "campaign-email", "ses-webhooks", "email-events")
ENV_QUEUE_URLS = {
    "transactional-email": "SQS_TRANSACTIONAL_EMAIL_QUEUE_URL",
    "campaign-email": "SQS_CAMPAIGN_EMAIL_QUEUE_URL",
    "ses-webhooks": "SQS_SES_WEBHOOKS_QUEUE_URL",
    "email-events": "SQS_EMAIL_EVENTS_QUEUE_URL",
}
SIMULATOR_RECIPIENTS = {
    "delivery": "success@simulator.amazonses.com",
    "bounce": "bounce@simulator.amazonses.com",
    "complaint": "complaint@simulator.amazonses.com",
}
EXPECTED_EVENT_TYPES = {
    "delivery": "delivered",
    "bounce": "bounce",
    "complaint": "complaint",
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
    inbound_dns_zone_id = (
        args.route53_hosted_zone_id
        or os.environ.get("INBOUND_MAIL_ROUTE53_ZONE_ID")
        or inbound_mail.get("dns_zone_id")
        or ""
    )
    inbound_dns_zone_name = (
        os.environ.get("INBOUND_MAIL_ROUTE53_ZONE_NAME") or inbound_mail.get("dns_zone_name") or inbound_domain
    )
    topic_arn = (
        args.ses_events_topic_arn or os.environ.get("SES_EVENTS_TOPIC_ARN") or outputs.get("ses_events_topic_arn") or ""
    )
    sender = args.sender or os.environ.get("DEFAULT_FROM_EMAIL") or ""

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
        "inbound_dns_zone_id": inbound_dns_zone_id,
        "inbound_dns_zone_name": inbound_dns_zone_name,
        "sender": sender,
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


def ses_identity_is_verified(sesv2, identity):
    try:
        response = sesv2.get_email_identity(EmailIdentity=identity)
    except ClientError:
        return False
    return response.get("VerifiedForSendingStatus", False) or response.get("VerificationStatus") == "SUCCESS"


def select_verified_sender(sesv2, sender, identities):
    candidates = []
    if sender:
        candidates.append(sender)
        if "@" in sender:
            candidates.append(sender.rsplit("@", 1)[1])
    candidates.extend(identities)

    verified_identities = []
    seen = set()
    for identity in candidates:
        if not identity or identity in seen:
            continue
        seen.add(identity)
        if ses_identity_is_verified(sesv2, identity):
            verified_identities.append(identity)

    if sender and (sender in verified_identities or ("@" in sender and sender.rsplit("@", 1)[1] in verified_identities)):
        return pass_("SES event sender preflight", f"sender={sender} verified_identity={verified_identities[0]}"), sender

    for identity in verified_identities:
        if "@" in identity:
            return pass_("SES event sender preflight", f"sender={identity} verified_identity={identity}"), identity

    detail = "no verified email sender identity found; skipping SES simulator sends"
    if sender:
        detail = f"sender={sender} is not verified and no verified email identity fallback was found; skipping SES simulator sends"
    return warn("SES event sender preflight", detail), ""


def send_simulator_email(ses, *, source, to_email, configuration_set, smoke_id):
    return ses.send_email(
        Source=source,
        Destination={"ToAddresses": [to_email]},
        Message={
            "Subject": {"Charset": "UTF-8", "Data": f"Datamailer SES event smoke {smoke_id}"},
            "Body": {
                "Text": {
                    "Charset": "UTF-8",
                    "Data": f"Datamailer SES event smoke {smoke_id}. This message targets the SES mailbox simulator.",
                }
            },
        },
        ConfigurationSetName=configuration_set,
    )["MessageId"]


def create_smoke_transactional_message(*, label, to_email, ses_message_id, smoke_id):
    timezone = importlib.import_module("django.utils.timezone")
    models = importlib.import_module("mailing.models")

    organization, _created = models.Organization.objects.get_or_create(
        slug="datamailer-smoke",
        defaults={"name": "Datamailer Smoke"},
    )
    client, _created = models.Client.objects.get_or_create(
        organization=organization,
        slug="ses-event-smoke",
        defaults={"name": "SES Event Smoke"},
    )
    template, _created = models.EmailTemplate.objects.get_or_create(
        client=client,
        key="ses-event-smoke",
        defaults={
            "name": "SES Event Smoke",
            "subject": "SES event smoke",
            "is_transactional": True,
        },
    )
    local_part, domain = to_email.split("@", 1)
    contact_email = f"datamailer-smoke+{smoke_id}-{label}-{local_part}@{domain}"
    contact = models.Contact.objects.create(email=contact_email, verified_at=timezone.now())
    return models.TransactionalMessage.objects.create(
        client=client,
        contact=contact,
        email=to_email,
        template=template,
        template_key=template.key,
        status=models.TransactionalMessageStatus.SENT,
        idempotency_key=f"ses-event-smoke-{smoke_id}-{label}",
        subject="SES event smoke",
        text_body="SES event smoke",
        ses_message_id=ses_message_id,
        sent_at=timezone.now(),
        metadata={"smoke_id": smoke_id, "simulator_recipient": to_email},
    )


def receive_ses_webhook_messages(sqs, queue_url, *, wait_seconds, batch_size=10, expected_count=None):
    deadline = time.monotonic() + wait_seconds
    messages = []
    while time.monotonic() < deadline:
        response = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=batch_size,
            WaitTimeSeconds=min(5, max(1, int(deadline - time.monotonic()))),
            VisibilityTimeout=30,
        )
        batch = response.get("Messages", [])
        if batch:
            messages.extend(batch)
            if expected_count is None:
                break
            if expected_count is not None and len(messages) >= expected_count:
                break
        else:
            time.sleep(1)
    return messages


def process_ses_webhook_messages(sqs, queue_url, messages):
    records_from_messages = importlib.import_module("mailing.sqs").records_from_messages
    ses_webhooks_handler = importlib.import_module("mailing.workers").ses_webhooks_handler

    if not messages:
        return {}
    response = ses_webhooks_handler(records_from_messages(messages), None)
    failed_ids = {item["itemIdentifier"] for item in response.get("batchItemFailures", [])}
    for message in messages:
        if message["MessageId"] not in failed_ids:
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
    return response


def verify_smoke_database_effects(messages_by_label):
    models = importlib.import_module("mailing.models")

    expected_statuses = {
        "delivery": models.TransactionalMessageStatus.SENT,
        "bounce": models.TransactionalMessageStatus.BOUNCED,
        "complaint": models.TransactionalMessageStatus.COMPLAINED,
    }
    expected_event_types = {
        "delivery": models.EmailEventType.DELIVERED,
        "bounce": models.EmailEventType.BOUNCE,
        "complaint": models.EmailEventType.COMPLAINT,
    }

    results = []
    for label, message in messages_by_label.items():
        message.refresh_from_db()
        event_exists = models.EmailEvent.objects.filter(
            transactional_message=message,
            event_type=expected_event_types[label],
        ).exists()
        if not event_exists:
            results.append(fail(f"SES event {label}", f"missing {EXPECTED_EVENT_TYPES[label]} event"))
            continue
        if message.status != expected_statuses[label]:
            results.append(fail(f"SES event {label}", f"status={message.status} expected={expected_statuses[label]}"))
            continue
        if label == "delivery" and message.delivered_at is None:
            results.append(fail("SES event delivery", "delivery event recorded but delivered_at is empty"))
            continue
        if label == "bounce" and message.contact.hard_bounced_at is None:
            message.contact.refresh_from_db()
            if message.contact.hard_bounced_at is None:
                results.append(fail("SES event bounce", "bounce event recorded but contact was not hard-bounced"))
                continue
        if label == "complaint" and message.contact.complained_at is None:
            message.contact.refresh_from_db()
            if message.contact.complained_at is None:
                results.append(fail("SES event complaint", "complaint event recorded but contact was not complained"))
                continue
        results.append(pass_(f"SES event {label}", f"ses_message_id={message.ses_message_id}"))
    return results


def smoke_database_effects_complete(messages_by_label):
    results = verify_smoke_database_effects(messages_by_label)
    return results and all(result.status != "FAIL" for result in results)


def run_ses_event_smoke(config, session, args):
    sesv2 = session.client("sesv2", region_name=config["region"])
    ses = session.client("ses", region_name=config["region"])
    sqs = session.client("sqs", region_name=config["region"])

    results = []
    configuration_set = config["ses_configuration_set"]
    if not configuration_set:
        return [fail("SES event configuration", "missing AWS_SES_CONFIGURATION_SET")]
    results.extend(check_ses_configuration_set(sesv2, configuration_set))
    preflight, sender = select_verified_sender(sesv2, config["sender"], config["ses_identities"])
    results.append(preflight)
    if not sender:
        return results

    queue_url = config["queue_urls"].get("ses-webhooks", "")
    if not queue_url:
        results.append(fail("SES event queue", "missing ses-webhooks queue URL"))
        return results

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "datamailer.settings")
    importlib.import_module("django").setup()

    smoke_id = f"{int(time.time())}-{uuid.uuid4().hex[:12]}"
    messages_by_label = {}
    for label, recipient in SIMULATOR_RECIPIENTS.items():
        try:
            ses_message_id = send_simulator_email(
                ses,
                source=sender,
                to_email=recipient,
                configuration_set=configuration_set,
                smoke_id=smoke_id,
            )
        except ClientError as exc:
            results.append(fail(f"SES simulator send {label}", f"{client_error_code(exc)} for {recipient}"))
            continue
        messages_by_label[label] = create_smoke_transactional_message(
            label=label,
            to_email=recipient,
            ses_message_id=ses_message_id,
            smoke_id=smoke_id,
        )
        results.append(pass_(f"SES simulator send {label}", f"recipient={recipient} ses_message_id={ses_message_id}"))

    if set(messages_by_label) != set(SIMULATOR_RECIPIENTS):
        return results

    processed_messages = 0
    failed_batch_items = []
    deadline = time.monotonic() + args.ses_event_timeout
    while time.monotonic() < deadline and not smoke_database_effects_complete(messages_by_label):
        raw_messages = receive_ses_webhook_messages(
            sqs,
            queue_url,
            wait_seconds=min(10, max(1, int(deadline - time.monotonic()))),
            expected_count=None,
        )
        if not raw_messages:
            continue
        worker_response = process_ses_webhook_messages(sqs, queue_url, raw_messages)
        processed_messages += len(raw_messages)
        failed_batch_items.extend(worker_response.get("batchItemFailures", []))

    if failed_batch_items:
        results.append(fail("SES webhook worker", f"failed_batch_items={failed_batch_items}"))
    elif processed_messages:
        results.append(pass_("SES webhook worker", f"processed_messages={processed_messages}"))
    else:
        results.append(fail("SES webhook worker", f"no messages observed within {args.ses_event_timeout}s"))

    results.extend(verify_smoke_database_effects(messages_by_label))
    for queue_name in REQUIRED_QUEUES:
        dlq_url = config["dlq_urls"].get(queue_name, "")
        if dlq_url:
            results.append(check_dlq(sqs, queue_name, dlq_url))
        else:
            results.append(warn(f"SQS DLQ {queue_name}", "missing DLQ URL; set dlq_urls from Terraform output to inspect it"))
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
    results.extend(check_route53(route53, config["inbound_dns_zone_name"], config["inbound_dns_zone_id"]))
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
    parser.add_argument("--sender", default="", help="Sender email for SES simulator sends. Defaults to DEFAULT_FROM_EMAIL.")
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
    parser.add_argument(
        "--ses-event-smoke",
        action="store_true",
        help="Run SES mailbox simulator send -> SNS/SQS -> worker/database smoke.",
    )
    parser.add_argument(
        "--ses-event-timeout",
        type=int,
        default=120,
        help="Seconds to wait for SES webhook SQS messages in --ses-event-smoke mode.",
    )
    return parser


def main(argv=None, session=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    session = session or boto3.Session(region_name=args.region or os.environ.get("AWS_REGION") or None)
    config = config_from_sources(args)
    results = run_ses_event_smoke(config, session, args) if args.ses_event_smoke else run_checks(config, session, args)

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
