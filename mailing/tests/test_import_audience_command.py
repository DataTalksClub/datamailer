import json
from pathlib import Path

import pytest
from django.core.management import call_command
from django.utils import timezone

from mailing.models import (
    Audience,
    Client,
    Contact,
    ContactTag,
    EmailValidationStatus,
    Organization,
    Subscription,
    SubscriptionStatus,
    Tag,
)

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).parent / "fixtures" / "import_audience"


@pytest.fixture
def organization():
    return Organization.objects.create(name="DataTalksClub", slug="datatalksclub")


@pytest.fixture
def audience(organization):
    return Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")


@pytest.fixture
def client_record(organization):
    return Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")


def run_import(capsys, fixture_name, organization, audience, client_record, *extra_args):
    call_command(
        "import_audience_csv",
        "--csv",
        str(FIXTURES / fixture_name),
        "--organization",
        organization.slug,
        "--audience",
        audience.slug,
        "--client",
        client_record.slug,
        *extra_args,
    )
    return json.loads(capsys.readouterr().out)


def test_dry_run_emits_report_without_database_writes(capsys, organization, audience, client_record):
    report = run_import(capsys, "valid_audience.csv", organization, audience, client_record, "--dry-run")

    assert report["dry_run"] is True
    assert report["columns"]["required"] == ["email"]
    assert "subscription_status" in report["columns"]["supported"]
    assert report["counts"]["rows_seen"] == 3
    assert report["counts"]["processed_rows"] == 3
    assert report["counts"]["created"] == 0
    assert report["counts"]["updated"] == 0
    assert report["row_results"][0]["action"] == "would_import"
    assert Contact.objects.count() == 0
    assert Subscription.objects.count() == 0
    assert Tag.objects.count() == 0
    assert ContactTag.objects.count() == 0


def test_report_can_be_written_to_operator_provided_path(
    capsys,
    tmp_path,
    organization,
    audience,
    client_record,
):
    report_path = tmp_path / "audience-import-report.json"

    call_command(
        "import_audience_csv",
        "--csv",
        str(FIXTURES / "valid_audience.csv"),
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
    assert report["dry_run"] is True
    assert report["counts"]["processed_rows"] == 3


def test_successful_import_creates_contacts_subscriptions_tags_and_states(
    capsys,
    organization,
    audience,
    client_record,
):
    report = run_import(capsys, "valid_audience.csv", organization, audience, client_record)

    assert report["counts"]["rows_seen"] == 3
    assert report["counts"]["created"] == 3
    assert report["counts"]["subscriptions_created"] == 3
    assert report["counts"]["tags_created"] == 3
    assert report["counts"]["tag_memberships_created"] == 4
    assert report["counts"]["verified_applied"] == 1
    assert report["counts"]["unsubscribed_applied"] == 1
    assert report["counts"]["global_unsubscribed_applied"] == 1
    assert report["counts"]["hard_bounced_applied"] == 1
    assert report["counts"]["complained_applied"] == 1
    assert report["counts"]["suppressed_applied"] == 1

    person = Contact.objects.get(normalized_email="person@example.com")
    person_subscription = Subscription.objects.get(contact=person, audience=audience, client=client_record)
    assert person.email == "Person@Example.COM"
    assert person.verified_at is not None
    assert person_subscription.status == SubscriptionStatus.SUBSCRIBED
    assert person_subscription.verified_at is not None
    assert set(person.tags.values_list("slug", flat=True)) == {"newsletter", "ml-zoomcamp"}

    unsubscribed = Contact.objects.get(normalized_email="unsubscribe@example.com")
    unsubscribed_subscription = Subscription.objects.get(contact=unsubscribed, audience=audience, client=client_record)
    assert unsubscribed_subscription.status == SubscriptionStatus.UNSUBSCRIBED
    assert unsubscribed_subscription.unsubscribe_reason == "user requested"

    suppressed = Contact.objects.get(normalized_email="suppressed@example.com")
    assert suppressed.global_unsubscribed_at is not None
    assert suppressed.hard_bounced_at is not None
    assert suppressed.complained_at is not None


def test_repeated_import_is_idempotent_and_reports_unchanged(capsys, organization, audience, client_record):
    first_report = run_import(capsys, "valid_audience.csv", organization, audience, client_record)
    second_report = run_import(capsys, "valid_audience.csv", organization, audience, client_record)

    assert first_report["counts"]["created"] == 3
    assert second_report["counts"]["created"] == 0
    assert second_report["counts"]["updated"] == 0
    assert second_report["counts"]["unchanged"] == 3
    assert second_report["counts"]["subscriptions_created"] == 0
    assert second_report["counts"]["tags_created"] == 0
    assert second_report["counts"]["tag_memberships_created"] == 0
    assert Contact.objects.count() == 3
    assert Subscription.objects.count() == 3
    assert Tag.objects.count() == 3
    assert ContactTag.objects.count() == 4


def test_import_can_set_email_validation_state_idempotently(capsys, organization, audience, client_record):
    first_report = run_import(capsys, "email_validation.csv", organization, audience, client_record)
    second_report = run_import(capsys, "email_validation.csv", organization, audience, client_record)

    assert first_report["counts"]["email_validation_applied"] == 2
    assert second_report["counts"]["email_validation_applied"] == 0
    assert second_report["counts"]["unchanged"] == 3

    valid = Contact.objects.get(normalized_email="valid@example.com")
    unknown = Contact.objects.get(normalized_email="unknown@example.com")
    invalid = Contact.objects.get(normalized_email="invalid@example.com")
    assert valid.email_validation_status == EmailValidationStatus.VALID
    assert valid.email_validation_reason == "syntax and provider check passed"
    assert valid.email_validated_at is not None
    assert unknown.email_validation_status == EmailValidationStatus.UNKNOWN
    assert unknown.email_validated_at is None
    assert invalid.email_validation_status == EmailValidationStatus.NO_MX
    assert invalid.email_validation_reason == "domain has no MX"


def test_duplicate_input_rows_use_first_valid_row_and_skip_later_duplicates(
    capsys,
    organization,
    audience,
    client_record,
):
    report = run_import(capsys, "duplicates.csv", organization, audience, client_record)

    assert report["counts"]["rows_seen"] == 3
    assert report["counts"]["processed_rows"] == 2
    assert report["counts"]["duplicate_input_rows"] == 1
    assert report["counts"]["skipped_rows"] == 1
    assert report["duplicate_rows"] == [
        {
            "row": 3,
            "email": "duplicate@example.COM",
            "normalized_email": "duplicate@example.com",
            "kept_row": 2,
            "action": "skipped",
        }
    ]

    duplicate = Contact.objects.get(normalized_email="duplicate@example.com")
    duplicate_subscription = Subscription.objects.get(contact=duplicate)
    assert duplicate_subscription.status == SubscriptionStatus.SUBSCRIBED
    assert set(duplicate.tags.values_list("slug", flat=True)) == {"first"}
    assert Contact.objects.count() == 2


def test_invalid_rows_are_reported_while_valid_rows_continue(capsys, organization, audience, client_record):
    report = run_import(capsys, "mixed_invalid.csv", organization, audience, client_record)

    assert report["counts"]["rows_seen"] == 6
    assert report["counts"]["invalid_rows"] == 4
    assert report["counts"]["processed_rows"] == 2
    assert report["counts"]["created"] == 2
    assert [row["row"] for row in report["invalid_rows"]] == [3, 4, 5, 6]
    assert Contact.objects.filter(normalized_email="valid-before@example.com").exists()
    assert Contact.objects.filter(normalized_email="valid-after@example.com").exists()
    assert Contact.objects.count() == 2


def test_existing_tags_are_reused_by_audience_scope(capsys, organization, audience, client_record):
    Tag.objects.create(audience=audience, name="Newsletter", slug="newsletter")
    other_audience = Audience.objects.create(organization=organization, name="Other", slug="other")
    Tag.objects.create(audience=other_audience, name="Newsletter", slug="newsletter")

    report = run_import(capsys, "valid_audience.csv", organization, audience, client_record)

    assert report["counts"]["tags_created"] == 2
    assert Tag.objects.filter(audience=audience, slug="newsletter").count() == 1
    assert Tag.objects.filter(slug="newsletter").count() == 2
    assert ContactTag.objects.filter(tag__audience=audience).count() == 4


def test_import_does_not_weaken_existing_unsubscribe_or_hard_suppression(
    capsys,
    organization,
    audience,
    client_record,
):
    existing_time = timezone.now()
    contact = Contact.objects.create(
        email="person@example.com",
        email_validation_status=EmailValidationStatus.VALID,
        email_validation_reason="pre-import check",
        email_validated_at=existing_time,
        global_unsubscribed_at=existing_time,
        hard_bounced_at=existing_time,
        complained_at=existing_time,
    )
    Subscription.objects.create(
        contact=contact,
        audience=audience,
        client=client_record,
        status=SubscriptionStatus.UNSUBSCRIBED,
        unsubscribed_at=existing_time,
        unsubscribe_reason="manual opt out",
    )

    report = run_import(capsys, "valid_audience.csv", organization, audience, client_record)

    contact.refresh_from_db()
    subscription = Subscription.objects.get(contact=contact, audience=audience, client=client_record)
    assert report["row_results"][0]["preserved_existing_opt_out"] is True
    assert subscription.status == SubscriptionStatus.UNSUBSCRIBED
    assert subscription.unsubscribe_reason == "manual opt out"
    assert contact.global_unsubscribed_at == existing_time
    assert contact.hard_bounced_at == existing_time
    assert contact.complained_at == existing_time
    assert contact.email_validation_status == EmailValidationStatus.VALID
    assert contact.email_validation_reason == "pre-import check"
    assert contact.email_validated_at == existing_time
