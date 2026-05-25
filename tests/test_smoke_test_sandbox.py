from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from scripts import smoke_test_sandbox as smoke


def aws_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "TestOperation")


class FakeSTS:
    def get_caller_identity(self):
        return {"Account": "817685572750", "Arn": "arn:aws:iam::817685572750:user/smoke"}


class FakeSQS:
    def __init__(self, missing_urls=None, dlq_depth=None):
        self.missing_urls = set(missing_urls or [])
        self.dlq_depth = dlq_depth or {}
        self.messages = {}
        self.deleted_bodies = []

    def get_queue_attributes(self, QueueUrl, AttributeNames):
        if QueueUrl in self.missing_urls:
            raise aws_error("AWS.SimpleQueueService.NonExistentQueue")
        queue_name = QueueUrl.rsplit("/", 1)[-1]
        is_dlq = queue_name.endswith("-dlq")
        base_name = queue_name.removeprefix("datamailer-sandbox-").removesuffix("-dlq")
        visible = self.dlq_depth.get(base_name, 0) if is_dlq else 0
        return {
            "Attributes": {
                "QueueArn": f"arn:aws:sqs:us-east-1:817685572750:{queue_name}",
                "ApproximateNumberOfMessages": str(visible),
                "ApproximateNumberOfMessagesNotVisible": "0",
                "MessageRetentionPeriod": "1209600",
            }
        }

    def send_message(self, QueueUrl, MessageBody, MessageAttributes):
        self.messages.setdefault(QueueUrl, []).append(
            {"MessageId": "sent-1", "ReceiptHandle": f"receipt-{len(self.messages)}", "Body": MessageBody}
        )
        return {"MessageId": "sent-1"}

    def receive_message(self, QueueUrl, MaxNumberOfMessages, WaitTimeSeconds, VisibilityTimeout, MessageAttributeNames):
        return {"Messages": self.messages.get(QueueUrl, [])[:MaxNumberOfMessages]}

    def delete_message(self, QueueUrl, ReceiptHandle):
        messages = self.messages.get(QueueUrl, [])
        for message in list(messages):
            if message["ReceiptHandle"] == ReceiptHandle:
                self.deleted_bodies.append(message["Body"])
                messages.remove(message)


class FakeSES:
    def __init__(self, missing_config=False, statuses=None):
        self.missing_config = missing_config
        self.statuses = statuses or {}

    def get_configuration_set(self, ConfigurationSetName):
        if self.missing_config:
            raise aws_error("ConfigurationSetDoesNotExist")
        return {"ConfigurationSet": {"Name": ConfigurationSetName}}

    def get_email_identity(self, EmailIdentity):
        status = self.statuses.get(EmailIdentity, "SUCCESS")
        return {
            "VerificationStatus": status,
            "VerifiedForSendingStatus": status == "SUCCESS",
        }


class FakeSNS:
    def __init__(self, missing_topic=False, subscriptions=None):
        self.missing_topic = missing_topic
        self.subscriptions = subscriptions or []

    def get_topic_attributes(self, TopicArn):
        if self.missing_topic:
            raise aws_error("NotFound")
        return {"Attributes": {"TopicArn": TopicArn}}

    def list_subscriptions_by_topic(self, TopicArn):
        return {"Subscriptions": [{"Endpoint": endpoint} for endpoint in self.subscriptions]}


class FakeS3:
    def __init__(self, missing_bucket=False, list_denied=False):
        self.missing_bucket = missing_bucket
        self.list_denied = list_denied

    def head_bucket(self, Bucket):
        if self.missing_bucket:
            raise aws_error("NoSuchBucket")
        return {}

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        if self.list_denied:
            raise aws_error("AccessDenied")
        return {"Contents": []}


class FakeRoute53:
    def __init__(self, has_zone=True):
        self.has_zone = has_zone

    def list_hosted_zones_by_name(self, DNSName, MaxItems):
        if not self.has_zone:
            return {"HostedZones": []}
        return {"HostedZones": [{"Id": "/hostedzone/Z123", "Name": f"{DNSName.rstrip('.')}."}]}

    def get_hosted_zone(self, Id):
        return {
            "HostedZone": {"Id": Id, "Name": "dtcdev.click."},
            "DelegationSet": {"NameServers": ["ns-1.example.net", "ns-2.example.net"]},
        }


class FakeSession:
    def __init__(self, sqs=None, ses=None, sns=None, s3=None, route53=None):
        self.sqs = sqs or FakeSQS()
        self.ses = ses or FakeSES()
        self.sns = sns or FakeSNS(subscriptions=[QUEUE_ARNS["ses-webhooks"]])
        self.s3 = s3 or FakeS3()
        self.route53 = route53 or FakeRoute53()

    def client(self, service_name, region_name=None):
        return {
            "sts": FakeSTS(),
            "sqs": self.sqs,
            "sesv2": self.ses,
            "sns": self.sns,
            "s3": self.s3,
            "route53": self.route53,
        }[service_name]


QUEUE_URLS = {
    name: f"https://sqs.us-east-1.amazonaws.com/817685572750/datamailer-sandbox-{name}"
    for name in smoke.REQUIRED_QUEUES
}
DLQ_URLS = {
    name: f"https://sqs.us-east-1.amazonaws.com/817685572750/datamailer-sandbox-{name}-dlq"
    for name in smoke.REQUIRED_QUEUES
}
QUEUE_ARNS = {name: f"arn:aws:sqs:us-east-1:817685572750:datamailer-sandbox-{name}" for name in smoke.REQUIRED_QUEUES}


def base_config():
    return {
        "region": "us-east-1",
        "queue_urls": dict(QUEUE_URLS),
        "queue_arns": dict(QUEUE_ARNS),
        "dlq_urls": dict(DLQ_URLS),
        "ses_configuration_set": "datamailer-sandbox",
        "ses_identities": ["sender@example.com"],
        "ses_events_topic_arn": "arn:aws:sns:us-east-1:817685572750:datamailer-sandbox-ses-events",
        "inbound_bucket": "datamailer-sandbox-817685572750-inbound-mail",
        "inbound_prefix": "raw/",
        "inbound_domain": "dtcdev.click",
    }


def args(**overrides):
    values = {
        "round_trip_all_queues": False,
        "require_inbound_s3_read": False,
        "route53_hosted_zone_id": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def statuses(results):
    return {result.name: result.status for result in results}


def test_success_path_deletes_transactional_smoke_message():
    sqs = FakeSQS()
    results = smoke.run_checks(base_config(), FakeSession(sqs=sqs), args())

    assert all(
        result.status == "PASS" for result in results if not result.name.startswith(("SES production", "Domain"))
    )
    assert statuses(results)["SQS round trip transactional-email"] == "PASS"
    assert sqs.deleted_bodies
    assert "sandbox_smoke_test" in sqs.deleted_bodies[0]


def test_warnings_do_not_make_main_exit_nonzero(monkeypatch, capsys):
    config = base_config()
    session = FakeSession(
        ses=FakeSES(statuses={"sender@example.com": "Pending"}),
        s3=FakeS3(list_denied=True),
        route53=FakeRoute53(has_zone=False),
    )
    monkeypatch.setattr(smoke, "config_from_sources", lambda parsed_args: config)

    exit_code = smoke.main([], session=session)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "WARN: SES identity sender@example.com: verification_status=Pending" in output
    assert "WARN: Inbound S3 list: AccessDenied" in output
    assert "WARN: Route53 hosted zone: no hosted zone found for dtcdev.click" in output


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        (FakeSession(sqs=FakeSQS(missing_urls={QUEUE_URLS["campaign-email"]})), "FAIL: SQS queue campaign-email"),
        (FakeSession(ses=FakeSES(missing_config=True)), "FAIL: SES configuration set"),
        (FakeSession(sns=FakeSNS(missing_topic=True)), "FAIL: SNS SES events topic"),
        (FakeSession(s3=FakeS3(missing_bucket=True)), "FAIL: Inbound S3 bucket"),
    ],
)
def test_required_resource_failures_make_main_exit_nonzero(monkeypatch, capsys, session, expected):
    monkeypatch.setattr(smoke, "config_from_sources", lambda parsed_args: base_config())

    exit_code = smoke.main([], session=session)
    output = capsys.readouterr().out

    assert exit_code == 1
    assert expected in output


def test_non_empty_dlq_is_warning_not_failure():
    results = smoke.run_checks(base_config(), FakeSession(sqs=FakeSQS(dlq_depth={"campaign-email": 2})), args())

    assert statuses(results)["SQS DLQ campaign-email"] == "WARN"
    assert not any(result.status == "FAIL" for result in results)


def test_s3_list_denied_can_be_required_failure():
    results = smoke.run_checks(
        base_config(), FakeSession(s3=FakeS3(list_denied=True)), args(require_inbound_s3_read=True)
    )

    assert statuses(results)["Inbound S3 list"] == "FAIL"
