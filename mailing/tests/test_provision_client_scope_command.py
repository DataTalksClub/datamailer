import pytest
from django.core.management import call_command

from mailing.models import Audience, Client, Organization

pytestmark = pytest.mark.django_db


def test_provision_client_scope_creates_expected_rows():
    call_command(
        "provision_client_scope",
        "--organization",
        "datatalksclub",
        "--organization-name",
        "DataTalksClub",
        "--audience",
        "dtc-courses",
        "--audience-name",
        "DataTalksClub Courses",
        "--client",
        "dtc-courses",
        "--client-name",
        "DTC Courses",
    )

    organization = Organization.objects.get(slug="datatalksclub")
    audience = Audience.objects.get(slug="dtc-courses")
    client = Client.objects.get(slug="dtc-courses")
    assert audience.organization == organization
    assert audience.name == "DataTalksClub Courses"
    assert client.organization == organization
    assert client.name == "DTC Courses"
    assert client.is_active is True


def test_provision_client_scope_is_idempotent_and_keeps_client_sender_config():
    organization = Organization.objects.create(name="Old Name", slug="datatalksclub")
    Client.objects.create(
        organization=organization,
        name="Old Client",
        slug="dtc-courses",
        default_sender_id="courses",
        sender_emails=[{"id": "courses", "email": "courses@dtcdev.click"}],
    )

    for _ in range(2):
        call_command(
            "provision_client_scope",
            "--organization",
            "datatalksclub",
            "--organization-name",
            "DataTalksClub",
            "--audience",
            "dtc-courses",
            "--audience-name",
            "DataTalksClub Courses",
            "--client",
            "dtc-courses",
            "--client-name",
            "DTC Courses",
        )

    assert Organization.objects.count() == 1
    assert Audience.objects.count() == 1
    assert Client.objects.count() == 1
    client = Client.objects.get(slug="dtc-courses")
    assert client.default_sender_id == "courses"
    assert client.sender_emails == [{"id": "courses", "email": "courses@dtcdev.click"}]


def test_provision_client_scope_adds_audience_for_existing_client_organizations():
    legacy_organization = Organization.objects.create(
        name="Legacy Organization",
        slug="legacy",
    )
    Client.objects.create(
        organization=legacy_organization,
        name="Existing DTC Courses",
        slug="dtc-courses",
    )

    call_command(
        "provision_client_scope",
        "--organization",
        "datatalksclub",
        "--organization-name",
        "DataTalksClub",
        "--audience",
        "dtc-courses",
        "--audience-name",
        "DataTalksClub Courses",
        "--client",
        "dtc-courses",
        "--client-name",
        "DTC Courses",
    )

    assert Audience.objects.filter(
        organization__slug="datatalksclub",
        slug="dtc-courses",
    ).exists()
    legacy_audience = Audience.objects.get(
        organization=legacy_organization,
        slug="dtc-courses",
    )
    assert legacy_audience.name == "DataTalksClub Courses"
