from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from django.utils import timezone

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


class FakeClassicSES:
    def __init__(self, fail_recipients=None):
        self.fail_recipients = set(fail_recipients or [])
        self.sent = []

    def send_email(self, **params):
        recipient = params["Destination"]["ToAddresses"][0]
        if recipient in self.fail_recipients:
            raise aws_error("MessageRejected")
        message_id = f"ses-{len(self.sent) + 1}"
        self.sent.append(params | {"MessageId": message_id})
        return {"MessageId": message_id}


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
    def __init__(self, sqs=None, ses=None, ses_classic=None, sns=None, s3=None, route53=None):
        self.sqs = sqs or FakeSQS()
        self.ses = ses or FakeSES()
        self.ses_classic = ses_classic or FakeClassicSES()
        self.sns = sns or FakeSNS(subscriptions=[QUEUE_ARNS["ses-webhooks"]])
        self.s3 = s3 or FakeS3()
        self.route53 = route53 or FakeRoute53()

    def client(self, service_name, region_name=None):
        return {
            "sts": FakeSTS(),
            "sqs": self.sqs,
            "sesv2": self.ses,
            "ses": self.ses_classic,
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
        "sender": "sender@example.com",
    }


def args(**overrides):
    values = {
        "round_trip_all_queues": False,
        "require_inbound_s3_read": False,
        "route53_hosted_zone_id": "",
        "ses_event_timeout": 1,
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


def test_ses_event_smoke_skips_before_send_when_sender_is_unverified():
    ses_classic = FakeClassicSES()
    config = base_config() | {"ses_identities": ["sender@example.com"]}
    session = FakeSession(
        ses=FakeSES(statuses={"sender@example.com": "PENDING", "example.com": "PENDING"}),
        ses_classic=ses_classic,
    )

    results = smoke.run_ses_event_smoke(config, session, args())

    assert statuses(results)["SES event sender preflight"] == "WARN"
    assert "skipping SES simulator sends" in results[-1].detail
    assert ses_classic.sent == []


def test_ses_event_smoke_uses_verified_identity_fallback():
    ses = FakeSES(
        statuses={"unverified@example.com": "PENDING", "example.com": "PENDING", "verified@example.com": "SUCCESS"}
    )

    result, sender = smoke.select_verified_sender(ses, "unverified@example.com", ["verified@example.com"])

    assert result.status == "PASS"
    assert sender == "verified@example.com"


def test_send_simulator_email_includes_configuration_set():
    ses = FakeClassicSES()

    message_id = smoke.send_simulator_email(
        ses,
        source="sender@example.com",
        to_email="success@simulator.amazonses.com",
        configuration_set="datamailer-sandbox",
        smoke_id="smoke-1",
    )

    assert message_id == "ses-1"
    assert ses.sent[0]["Source"] == "sender@example.com"
    assert ses.sent[0]["Destination"] == {"ToAddresses": ["success@simulator.amazonses.com"]}
    assert ses.sent[0]["ConfigurationSetName"] == "datamailer-sandbox"


@pytest.mark.django_db
def test_ses_event_smoke_processes_raw_sns_events_and_verifies_database():
    ses_classic = FakeClassicSES()
    sqs = FakeSesEventSQS(ses_classic)
    session = FakeSession(sqs=sqs, ses=FakeSES(statuses={"sender@example.com": "SUCCESS"}), ses_classic=ses_classic)

    results = smoke.run_ses_event_smoke(base_config(), session, args(ses_event_timeout=1))

    result_statuses = statuses(results)
    assert result_statuses["SES event sender preflight"] == "PASS"
    assert result_statuses["SES simulator send delivery"] == "PASS"
    assert result_statuses["SES simulator send bounce"] == "PASS"
    assert result_statuses["SES simulator send complaint"] == "PASS"
    assert result_statuses["SES webhook worker"] == "PASS"
    assert result_statuses["SES event delivery"] == "PASS"
    assert result_statuses["SES event bounce"] == "PASS"
    assert result_statuses["SES event complaint"] == "PASS"
    assert [sent["ConfigurationSetName"] for sent in ses_classic.sent] == ["datamailer-sandbox"] * 3
    assert len(sqs.deleted_receipts) == 3


@pytest.mark.django_db
def test_verify_smoke_database_effects_reports_missing_event():
    message = smoke.create_smoke_transactional_message(
        label="delivery",
        to_email="success@simulator.amazonses.com",
        ses_message_id="ses-missing",
        smoke_id="missing",
    )

    results = smoke.verify_smoke_database_effects({"delivery": message})

    assert statuses(results)["SES event delivery"] == "FAIL"
    assert "missing delivered event" in results[0].detail


class FakeSesEventSQS(FakeSQS):
    def __init__(self, ses_classic):
        super().__init__()
        self.ses_classic = ses_classic
        self.generated = False
        self.deleted_receipts = []

    def receive_message(self, QueueUrl, MaxNumberOfMessages, WaitTimeSeconds, VisibilityTimeout, MessageAttributeNames=None):
        if not self.generated and self.ses_classic.sent:
            self.messages[QueueUrl] = [
                {
                    "MessageId": f"sqs-{index}",
                    "ReceiptHandle": f"receipt-{index}",
                    "Body": raw_sns_body(sent, index),
                }
                for index, sent in enumerate(self.ses_classic.sent, start=1)
            ]
            self.generated = True
        return {"Messages": self.messages.get(QueueUrl, [])[:MaxNumberOfMessages]}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted_receipts.append(ReceiptHandle)
        super().delete_message(QueueUrl, ReceiptHandle)


def raw_sns_body(sent, index):
    recipient = sent["Destination"]["ToAddresses"][0]
    event_type = {
        "success@simulator.amazonses.com": "Delivery",
        "bounce@simulator.amazonses.com": "Bounce",
        "complaint@simulator.amazonses.com": "Complaint",
    }[recipient]
    detail = {}
    if event_type == "Bounce":
        detail = {"bounceType": "Permanent", "bounceSubType": "General"}
    elif event_type == "Complaint":
        detail = {"complaintFeedbackType": "abuse"}
    ses_payload = {
        "eventType": event_type,
        "mail": {
            "timestamp": timezone.now().isoformat(),
            "source": sent["Source"],
            "messageId": sent["MessageId"],
        },
        event_type.casefold(): detail,
    }
    return smoke.json.dumps(
        {
            "Type": "Notification",
            "MessageId": f"sns-{index}",
            "TopicArn": "arn:aws:sns:us-east-1:817685572750:datamailer-sandbox-ses-events",
            "Message": smoke.json.dumps(ses_payload),
            "Timestamp": timezone.now().isoformat(),
        }
    )
