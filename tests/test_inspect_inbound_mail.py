from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from scripts import inspect_inbound_mail as inspect


def aws_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "TestOperation")


class Args:
    expect_to = []
    expect_from = []
    expect_subject = []
    expect_body = []
    expect_unsubscribe_link = False
    expect_tracking_substring = []


class FakeS3:
    def __init__(self, *, list_denied=False):
        self.list_denied = list_denied
        self.objects = [
            {
                "Key": "raw/old.eml",
                "LastModified": datetime(2026, 5, 24, tzinfo=UTC),
                "Body": b"old",
            },
            {
                "Key": "raw/new.eml",
                "LastModified": datetime(2026, 5, 25, tzinfo=UTC),
                "Body": b"new",
            },
        ]
        self.fetched = []

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        if self.list_denied:
            raise aws_error("AccessDenied")
        return {"Contents": [item for item in self.objects if item["Key"].startswith(Prefix)][:MaxKeys]}

    def get_object(self, Bucket, Key):
        self.fetched.append((Bucket, Key))
        for item in self.objects:
            if item["Key"] == Key:
                return {"Body": BytesIO(item["Body"])}
        raise KeyError(Key)


class FakeSession:
    def __init__(self, s3):
        self.s3 = s3

    def client(self, service_name, region_name=None):
        assert service_name == "s3"
        return self.s3


def parse(raw):
    return inspect.parse_mime(raw.encode())


def test_parse_text_only_message():
    summary = parse(
        """From: Sender <sender@example.com>
To: Datamailer <datamailer@dtcdev.click>
Subject: Text only
Message-ID: <text@example.com>
Content-Type: text/plain; charset="utf-8"

Plain body with https://example.com/plain.
"""
    )

    assert summary.subject == "Text only"
    assert summary.text.count == 1
    assert summary.html.count == 0
    assert summary.links == ["https://example.com/plain"]


def test_parse_html_only_message():
    summary = parse(
        """From: sender@example.com
To: datamailer@dtcdev.click
Subject: HTML only
Content-Type: text/html; charset="utf-8"

<p>HTML body</p><a href="https://example.com/html">HTML</a>
"""
    )

    assert summary.text.count == 0
    assert summary.html.count == 1
    assert "https://example.com/html" in summary.links


def test_parse_multipart_alternative_message():
    summary = parse(
        """From: sender@example.com
To: datamailer@dtcdev.click
Subject: Alternative
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="alt"

--alt
Content-Type: text/plain; charset="utf-8"

Plain alternative https://example.com/plain
--alt
Content-Type: text/html; charset="utf-8"

<a href="https://example.com/html">HTML</a>
--alt--
"""
    )

    assert summary.text.count == 1
    assert summary.html.count == 1
    assert "https://example.com/plain" in summary.links
    assert "https://example.com/html" in summary.links


def test_parse_multipart_mixed_ignores_attachment():
    summary = parse(
        """From: sender@example.com
To: datamailer@dtcdev.click
Subject: Mixed
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="mixed"

--mixed
Content-Type: text/plain; charset="utf-8"

Visible body.
--mixed
Content-Type: text/plain; charset="utf-8"
Content-Disposition: attachment; filename="notes.txt"

Attachment text should not be summarized.
--mixed--
"""
    )

    assert summary.text.count == 1
    assert "Visible body" in summary.body_text
    assert "Attachment text" not in summary.body_text


def test_assertion_success_paths():
    summary = inspect.parse_mime(Path("tests/fixtures/inbound/sample.eml").read_bytes())
    args = Args()
    args.expect_to = ["datamailer@dtcdev.click"]
    args.expect_from = ["sender@example.com"]
    args.expect_subject = ["inbound smoke"]
    args.expect_body = ["validates inbound MIME"]
    args.expect_unsubscribe_link = True
    args.expect_tracking_substring = ["track.example.com"]

    results = inspect.assertion_results(summary, args)

    assert all(result.status == "PASS" for result in results)


def test_assertion_failure_paths():
    summary = parse(
        """From: sender@example.com
To: datamailer@dtcdev.click
Subject: Hello
Content-Type: text/plain; charset="utf-8"

Body.
"""
    )
    args = Args()
    args.expect_to = ["other@example.com"]
    args.expect_from = ["other-sender@example.com"]
    args.expect_subject = ["missing"]
    args.expect_body = ["required"]
    args.expect_unsubscribe_link = True
    args.expect_tracking_substring = ["track.example.com"]

    results = inspect.assertion_results(summary, args)

    assert {result.status for result in results} == {"FAIL"}


def test_cli_fixture_mode_passes(capsys):
    exit_code = inspect.main(
        [
            "--fixture",
            "tests/fixtures/inbound/sample.eml",
            "--expect-to",
            "datamailer@dtcdev.click",
            "--expect-subject",
            "inbound smoke",
            "--expect-body",
            "validates inbound MIME",
            "--expect-unsubscribe-link",
            "--expect-tracking-substring",
            "track.example.com",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "PASS: Subject: Datamailer inbound smoke" in output
    assert "PASS: Expected recipient: datamailer@dtcdev.click" in output


def test_cli_fixture_mode_fails_on_failed_assertion(capsys):
    exit_code = inspect.main(
        [
            "--fixture",
            "tests/fixtures/inbound/sample.eml",
            "--expect-to",
            "other@example.com",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: Expected recipient: expected=other@example.com" in output


def test_cli_fixture_mode_fails_on_malformed_input(tmp_path, capsys):
    fixture = tmp_path / "malformed.eml"
    fixture.write_bytes(b"body without headers")

    exit_code = inspect.main(["--fixture", str(fixture)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: Inbound MIME inspection" in output
    assert "MIME input has no headers" in output


def test_s3_latest_fetches_newest_object():
    s3 = FakeS3()
    args = inspect.build_parser().parse_args(["--latest", "--inbound-bucket", "bucket", "--inbound-prefix", "raw/"])

    raw, source = inspect.read_input(args, session=FakeSession(s3))

    assert raw == b"new"
    assert source == "s3://bucket/raw/new.eml"
    assert s3.fetched == [("bucket", "raw/new.eml")]


def test_s3_explicit_key_fetches_exact_object():
    s3 = FakeS3()
    args = inspect.build_parser().parse_args(["--s3-key", "raw/old.eml", "--inbound-bucket", "bucket"])

    raw, source = inspect.read_input(args, session=FakeSession(s3))

    assert raw == b"old"
    assert source == "s3://bucket/raw/old.eml"
    assert s3.fetched == [("bucket", "raw/old.eml")]


def test_s3_empty_prefix_fails_clearly():
    args = inspect.build_parser().parse_args(["--latest", "--inbound-bucket", "bucket", "--inbound-prefix", "empty/"])

    with pytest.raises(RuntimeError, match="no inbound S3 objects found"):
        inspect.read_input(args, session=FakeSession(FakeS3()))


def test_s3_permission_denied_fails_clearly(capsys):
    exit_code = inspect.main(
        ["--latest", "--inbound-bucket", "bucket", "--inbound-prefix", "raw/"],
        session=FakeSession(FakeS3(list_denied=True)),
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: Inbound MIME inspection: AccessDenied" in output


def test_s3_missing_explicit_key_fails_clearly(capsys):
    exit_code = inspect.main(["--s3-key", "raw/missing.eml", "--inbound-bucket", "bucket"], session=FakeSession(FakeS3()))

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL: Inbound MIME inspection" in output
    assert "raw/missing.eml" in output
