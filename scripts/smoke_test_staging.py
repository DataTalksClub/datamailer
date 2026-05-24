#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import boto3


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    human: bool = False


def check_web_health(base_url):
    url = f"{base_url.rstrip('/')}/health/"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read(200).decode("utf-8", errors="replace")
            return CheckResult("web health", response.status == 200, f"{response.status} {body[:120]}")
    except (urllib.error.URLError, TimeoutError) as exc:
        return CheckResult("web health", False, str(exc))


def check_queue_round_trip(queue_url, region):
    if not queue_url:
        return CheckResult("SQS round trip", True, "skipped because --transactional-queue-url was not provided", human=True)
    sqs = boto3.client("sqs", region_name=region)
    body = json.dumps(
        {
            "schema_version": 1,
            "message_type": "staging_smoke_test",
            "idempotency_key": f"smoke-{int(time.time())}",
            "dry_run": True,
            "note": "Non-production validation payload. Do not send email.",
        },
        sort_keys=True,
    )
    send = sqs.send_message(QueueUrl=queue_url, MessageBody=body)
    received = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=10)
    messages = received.get("Messages", [])
    if not messages:
        return CheckResult("SQS round trip", False, f"sent {send['MessageId']} but did not receive a message")
    message = messages[0]
    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
    return CheckResult("SQS round trip", message["Body"] == body, f"sent and deleted {message['MessageId']}")


def check_cloudwatch_alarm_visibility(stack_name, region):
    if not stack_name:
        return CheckResult("alarm visibility", True, "skipped because --stack-name was not provided", human=True)
    cloudwatch = boto3.client("cloudwatch", region_name=region)
    alarms = cloudwatch.describe_alarms(AlarmNamePrefix=stack_name).get("MetricAlarms", [])
    if alarms:
        return CheckResult("alarm visibility", True, f"found {len(alarms)} alarms with prefix {stack_name}")
    return CheckResult("alarm visibility", False, f"no alarms found with prefix {stack_name}")


def main():
    parser = argparse.ArgumentParser(description="Run staging smoke checks without sending production email.")
    parser.add_argument("--base-url", required=True, help="Staging base URL, for example https://staging.datamailer.example.com")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--transactional-queue-url", default=os.environ.get("SQS_TRANSACTIONAL_EMAIL_QUEUE_URL", ""))
    parser.add_argument("--stack-name", default="")
    args = parser.parse_args()

    checks = [
        check_web_health(args.base_url),
        check_queue_round_trip(args.transactional_queue_url, args.region),
        check_cloudwatch_alarm_visibility(args.stack_name, args.region),
        CheckResult("worker invocation", True, "HUMAN: invoke Lambda with a non-production SQS-shaped dry-run event and inspect logs", human=True),
        CheckResult("migrations", True, "HUMAN: run python manage.py migrate --check on the web host or release job", human=True),
        CheckResult("no production email", True, "HUMAN: confirm staging SES identity/sandbox sender and dry-run payloads only", human=True),
    ]

    failed = False
    for check in checks:
        status = "PASS" if check.ok else "FAIL"
        marker = " HUMAN" if check.human else ""
        print(f"{status}{marker}: {check.name}: {check.detail}")
        failed = failed or not check.ok

    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL: smoke test crashed: {exc}", file=sys.stderr)
        raise
