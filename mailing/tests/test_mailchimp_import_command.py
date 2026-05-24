import csv
import io
import json
import zipfile

import pytest
from django.core.management import call_command
from django.utils import timezone

from mailing.models import (
    Audience,
    Client,
    Contact,
    ContactSourceMetadata,
    EmailValidationStatus,
    Organization,
    Subscription,
    SubscriptionStatus,
    Tag,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="Newsletter", slug="newsletter")


@pytest.fixture
def client_record(organization):
    return Client.objects.create(organization=organization, name="Newsletter App", slug="newsletter-app")


def test_mailchimp_import_dry_run_reports_without_writes(capsys, tmp_path, organization, audience, client_record):
    zip_path = synthetic_mailchimp_zip(tmp_path)

    call_command(
        "import_mailchimp_zip",
        "--zip",
        str(zip_path),
        "--organization",
        organization.slug,
        "--audience",
        audience.slug,
        "--client",
        client_record.slug,
        "--dry-run",
    )

    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["counts"]["files_seen"] == 3
    assert report["counts"]["rows_seen"] == 4
    assert report["counts"]["processed_rows"] == 3
    assert report["counts"]["invalid_rows"] == 1
    assert report["category_counts"] == {"subscribed": 1, "unsubscribed": 2, "cleaned": 1}
    assert "subscriber_hash" in report["row_results"][0]
    assert "email" not in report["row_results"][0]
    assert_report_has_no_row_pii(report)
    assert Contact.objects.count() == 0
    assert Subscription.objects.count() == 0
    assert ContactSourceMetadata.objects.count() == 0


def test_mailchimp_import_maps_statuses_tags_and_metadata_idempotently(
    capsys,
    tmp_path,
    organization,
    audience,
    client_record,
):
    zip_path = synthetic_mailchimp_zip(tmp_path)

    call_command(
        "import_mailchimp_zip",
        "--zip",
        str(zip_path),
        "--organization",
        organization.slug,
        "--audience",
        audience.slug,
        "--client",
        client_record.slug,
    )
    first_report = json.loads(capsys.readouterr().out)

    call_command(
        "import_mailchimp_zip",
        "--zip",
        str(zip_path),
        "--organization",
        organization.slug,
        "--audience",
        audience.slug,
        "--client",
        client_record.slug,
    )
    second_report = json.loads(capsys.readouterr().out)

    assert first_report["counts"]["created"] == 3
    assert first_report["counts"]["updated"] == 0
    assert first_report["counts"]["invalid_rows"] == 1
    assert second_report["counts"]["created"] == 0
    assert second_report["counts"]["updated"] == 0
    assert second_report["counts"]["unchanged"] == 3
    assert_report_has_no_row_pii(first_report)
    assert_report_has_no_row_pii(second_report)

    subscribed = Contact.objects.get(normalized_email="subscribed@example.com")
    subscribed_subscription = Subscription.objects.get(contact=subscribed, audience=audience, client=client_record)
    subscribed_metadata = ContactSourceMetadata.objects.get(contact=subscribed, audience=audience, client=client_record)
    assert subscribed_subscription.status == SubscriptionStatus.SUBSCRIBED
    assert subscribed_subscription.verified_at is not None
    assert subscribed.verified_at is not None
    assert subscribed.email_validation_status == EmailValidationStatus.EXTERNALLY_VALIDATED
    assert set(subscribed.tags.values_list("slug", flat=True)) == {"newsletter", "vip"}
    assert subscribed_metadata.external_id == "euid-sub"
    assert subscribed_metadata.metadata["mailchimp"]["leid"] == "leid-sub"
    assert subscribed_metadata.metadata["mailchimp"]["notes"] == "synthetic note"
    assert subscribed_metadata.metadata["mailchimp"]["timezone"] == "Europe/Berlin"
    assert subscribed_metadata.metadata["mailchimp"]["tags"] == ["Newsletter", "VIP"]

    unsubscribed = Contact.objects.get(normalized_email="unsubscribed@example.com")
    unsubscribed_subscription = Subscription.objects.get(contact=unsubscribed, audience=audience, client=client_record)
    assert unsubscribed_subscription.status == SubscriptionStatus.UNSUBSCRIBED
    assert unsubscribed_subscription.unsubscribed_at is not None
    assert "camp-1" in unsubscribed_subscription.unsubscribe_reason
    assert unsubscribed.global_unsubscribed_at is not None

    cleaned = Contact.objects.get(normalized_email="cleaned@example.com")
    cleaned_subscription = Subscription.objects.get(contact=cleaned, audience=audience, client=client_record)
    assert cleaned_subscription.status == SubscriptionStatus.UNSUBSCRIBED
    assert cleaned.hard_bounced_at is not None
    assert cleaned.email_validation_status == EmailValidationStatus.MANUALLY_INVALID
    assert cleaned.email_validation_reason == "mailchimp_cleaned"
    assert Tag.objects.filter(audience=audience, slug="bounced").exists()


def test_mailchimp_import_preserves_existing_stronger_suppression(
    capsys,
    tmp_path,
    organization,
    audience,
    client_record,
):
    existing_time = timezone.now()
    contact = Contact.objects.create(
        email="subscribed@example.com",
        global_unsubscribed_at=existing_time,
        hard_bounced_at=existing_time,
        email_validation_status=EmailValidationStatus.MANUALLY_INVALID,
        email_validation_reason="manual operator review",
        email_validated_at=existing_time,
    )
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=client_record,
        status=SubscriptionStatus.UNSUBSCRIBED,
        unsubscribed_at=existing_time,
        unsubscribe_reason="manual opt out",
    )
    zip_path = synthetic_mailchimp_zip(tmp_path, subscribed_only=True)

    call_command(
        "import_mailchimp_zip",
        "--zip",
        str(zip_path),
        "--organization",
        organization.slug,
        "--audience",
        audience.slug,
        "--client",
        client_record.slug,
    )
    capsys.readouterr()

    contact.refresh_from_db()
    subscription = Subscription.objects.get(contact=contact, audience=audience, client=client_record)
    assert subscription.status == SubscriptionStatus.UNSUBSCRIBED
    assert subscription.unsubscribe_reason == "manual opt out"
    assert contact.global_unsubscribed_at == existing_time
    assert contact.hard_bounced_at == existing_time
    assert contact.email_validation_status == EmailValidationStatus.MANUALLY_INVALID
    assert contact.email_validation_reason == "manual operator review"


def test_mailchimp_import_report_can_be_written_to_path(capsys, tmp_path, organization, audience, client_record):
    zip_path = synthetic_mailchimp_zip(tmp_path, subscribed_only=True)
    report_path = tmp_path / "mailchimp-report.json"

    call_command(
        "import_mailchimp_zip",
        "--zip",
        str(zip_path),
        "--organization",
        organization.slug,
        "--audience",
        audience.slug,
        "--client",
        client_record.slug,
        "--dry-run",
        "--report",
        str(report_path),
    )

    assert capsys.readouterr().out == ""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["counts"]["rows_seen"] == 1
    report_text = report_path.read_text(encoding="utf-8")
    assert "subscribed@example.com" not in report_text
    assert "Synthetic Subscriber" not in report_text
    assert "192.0.2.10" not in report_text
    assert "synthetic note" not in report_text


def test_mailchimp_import_stdout_report_has_no_row_pii(capsys, tmp_path, organization, audience, client_record):
    zip_path = synthetic_mailchimp_zip(tmp_path)

    call_command(
        "import_mailchimp_zip",
        "--zip",
        str(zip_path),
        "--organization",
        organization.slug,
        "--audience",
        audience.slug,
        "--client",
        client_record.slug,
    )

    output = capsys.readouterr().out
    assert "subscribed@example.com" not in output
    assert "unsubscribed@example.com" not in output
    assert "cleaned@example.com" not in output
    assert "not-an-email" not in output
    assert "Synthetic Subscriber" not in output
    assert "Synthetic Unsubscribed" not in output
    assert "Synthetic Cleaned" not in output
    assert "192.0.2.10" not in output
    assert "192.0.2.20" not in output
    assert "synthetic note" not in output
    assert "unsubscribe note" not in output
    assert "subscriber_hash" in output


def synthetic_mailchimp_zip(tmp_path, *, subscribed_only=False):
    zip_path = tmp_path / "synthetic-mailchimp-export.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(
            "subscribed_email_audience_export_test.csv",
            csv_text(
                [
                    "Name",
                    "Email Address",
                    "MEMBER_RATING",
                    "OPTIN_TIME",
                    "OPTIN_IP",
                    "CONFIRM_TIME",
                    "CONFIRM_IP",
                    "GMTOFF",
                    "DSTOFF",
                    "TIMEZONE",
                    "CC",
                    "REGION",
                    "LAST_CHANGED",
                    "LEID",
                    "EUID",
                    "NOTES",
                    "TAGS",
                ],
                [
                    [
                        "Synthetic Subscriber",
                        "subscribed@example.com",
                        "5",
                        "2024-01-01 09:00:00",
                        "192.0.2.10",
                        "2024-01-01 09:05:00",
                        "192.0.2.11",
                        "1",
                        "0",
                        "Europe/Berlin",
                        "DE",
                        "Berlin",
                        "2024-01-02 10:00:00",
                        "leid-sub",
                        "euid-sub",
                        "synthetic note",
                        "Newsletter, VIP",
                    ],
                ],
            ),
        )
        if subscribed_only:
            return zip_path
        archive.writestr(
            "unsubscribed_email_audience_export_test.csv",
            csv_text(
                [
                    "Name",
                    "Email Address",
                    "MEMBER_RATING",
                    "OPTIN_TIME",
                    "OPTIN_IP",
                    "CONFIRM_TIME",
                    "CONFIRM_IP",
                    "GMTOFF",
                    "DSTOFF",
                    "TIMEZONE",
                    "CC",
                    "REGION",
                    "UNSUB_TIME",
                    "UNSUB_CAMPAIGN_TITLE",
                    "UNSUB_CAMPAIGN_ID",
                    "UNSUB_REASON",
                    "UNSUB_REASON_OTHER",
                    "LEID",
                    "EUID",
                    "NOTES",
                    "TAGS",
                ],
                [
                    [
                        "Synthetic Unsubscribed",
                        "unsubscribed@example.com",
                        "2",
                        "2024-02-01 09:00:00",
                        "192.0.2.20",
                        "2024-02-01 09:05:00",
                        "192.0.2.21",
                        "1",
                        "0",
                        "Europe/Berlin",
                        "DE",
                        "Berlin",
                        "2024-02-03 10:00:00",
                        "Campaign",
                        "camp-1",
                        "normal_unsubscribe",
                        "",
                        "leid-unsub",
                        "euid-unsub",
                        "unsubscribe note",
                        "Newsletter",
                    ],
                    ["Invalid", "not-an-email", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
                ],
            ),
        )
        archive.writestr(
            "cleaned_email_audience_export_test.csv",
            csv_text(
                [
                    "Name",
                    "Email Address",
                    "MEMBER_RATING",
                    "OPTIN_TIME",
                    "OPTIN_IP",
                    "CONFIRM_TIME",
                    "CONFIRM_IP",
                    "GMTOFF",
                    "DSTOFF",
                    "TIMEZONE",
                    "CC",
                    "REGION",
                    "CLEAN_TIME",
                    "CLEAN_CAMPAIGN_TITLE",
                    "CLEAN_CAMPAIGN_ID",
                    "LEID",
                    "EUID",
                    "NOTES",
                    "TAGS",
                ],
                [
                    [
                        "Synthetic Cleaned",
                        "cleaned@example.com",
                        "1",
                        "2024-03-01 09:00:00",
                        "192.0.2.30",
                        "2024-03-01 09:05:00",
                        "192.0.2.31",
                        "1",
                        "0",
                        "Europe/Berlin",
                        "DE",
                        "Berlin",
                        "2024-03-04 10:00:00",
                        "Bounce Campaign",
                        "camp-clean",
                        "leid-clean",
                        "euid-clean",
                        "cleaned note",
                        "Bounced",
                    ],
                ],
            ),
        )
    return zip_path


def csv_text(headers, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue()


def assert_report_has_no_row_pii(report):
    serialized = json.dumps(report)
    for value in (
        "subscribed@example.com",
        "unsubscribed@example.com",
        "cleaned@example.com",
        "not-an-email",
        "Synthetic Subscriber",
        "Synthetic Unsubscribed",
        "Synthetic Cleaned",
        "192.0.2.10",
        "192.0.2.20",
        "192.0.2.30",
        "synthetic note",
        "unsubscribe note",
        "cleaned note",
    ):
        assert value not in serialized
