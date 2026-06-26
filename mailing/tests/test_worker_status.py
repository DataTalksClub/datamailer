from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    CmpCallback,
    CmpCallbackStatus,
    Contact,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.worker_status import sandbox_worker_statuses, systemd_service_properties

pytestmark = pytest.mark.django_db


def test_systemd_status_can_be_disabled(settings, monkeypatch):
    settings.WORKER_STATUS_SYSTEMD_ENABLED = False

    def fail_run(*args, **kwargs):
        raise AssertionError("systemctl should not be called")

    monkeypatch.setattr("mailing.services.worker_status.subprocess.run", fail_run)

    assert systemd_service_properties("datamailer-campaign-worker.service") == {
        "ActiveState": "unknown",
        "UnavailableReason": "Systemd status checks are disabled.",
    }


def test_sandbox_worker_statuses_include_systemd_state_and_local_backlog(settings, monkeypatch):
    settings.WORKER_STATUS_SYSTEMD_ENABLED = True
    _create_worker_backlog()

    monkeypatch.setattr("mailing.services.worker_status.subprocess.run", _fake_systemd_run)

    statuses = {status.key: status for status in sandbox_worker_statuses()}

    assert statuses["transactional"].badge_label == "Running"
    assert statuses["transactional"].backlog_count == 1
    assert statuses["transactional"].pid == "123"
    assert statuses["campaign"].badge_label == "Failed"
    assert statuses["campaign"].badge_tone == "danger"
    assert statuses["campaign"].detail == "failed; result=exit-code"
    assert statuses["campaign"].backlog_count == 1
    assert statuses["ses-webhooks"].backlog_count is None
    assert statuses["cmp-callbacks"].backlog_count == 1


def test_worker_status_api_returns_staff_only_json_status(client, settings, monkeypatch):
    settings.WORKER_STATUS_SYSTEMD_ENABLED = True
    _create_worker_backlog()
    monkeypatch.setattr("mailing.services.worker_status.subprocess.run", _fake_systemd_run)
    operator = get_user_model().objects.create_user("operator", "operator@example.com", "password", is_staff=True)
    client.force_login(operator)

    response = client.get(reverse("mailing:api_worker_status"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    workers = {worker["key"]: worker for worker in payload["workers"]}
    assert workers["transactional"]["alive"] is True
    assert workers["transactional"]["backlog"] == {"label": "Queued messages", "count": 1}
    assert workers["campaign"]["alive"] is False
    assert workers["campaign"]["status"] == "failed"
    assert workers["campaign"]["detail"] == "failed; result=exit-code"
    assert workers["ses-webhooks"]["backlog"] == {"label": "SQS backlog", "count": None}


def test_worker_status_api_requires_staff(client):
    response = client.get(reverse("mailing:api_worker_status"))

    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]


def _fake_systemd_run(args, **kwargs):
    service_name = args[2]
    if service_name == "datamailer-campaign-worker.service":
        stdout = "\n".join(
            [
                "LoadState=loaded",
                "ActiveState=failed",
                "SubState=failed",
                "Result=exit-code",
                "MainPID=0",
                "NRestarts=3",
            ]
        )
    else:
        stdout = "\n".join(
            [
                "LoadState=loaded",
                "ActiveState=active",
                "SubState=running",
                "Result=success",
                "MainPID=123",
                "ExecMainStartTimestamp=Fri 2026-06-26 09:00:00 UTC",
                "NRestarts=1",
            ]
        )
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _create_worker_backlog():
    organization = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    client = Client.objects.create(organization=organization, name="DTC Courses", slug="dtc-courses")
    audience = Audience.objects.create(organization=organization, name="DataTalksClub", slug="datatalks-club")
    contact = Contact.objects.create(email="learner@example.com")
    template = EmailTemplate.objects.create(
        client=client,
        key="welcome",
        name="Welcome",
        subject="Welcome",
        is_transactional=True,
    )
    TransactionalMessage.objects.create(
        client=client,
        contact=contact,
        email=contact.email,
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.QUEUED,
        subject="Welcome",
    )
    campaign = Campaign.objects.create(
        audience=audience,
        client=client,
        subject="Campaign",
        status=CampaignStatus.QUEUED,
    )
    CampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact,
        email=contact.email,
        status=CampaignRecipientStatus.PENDING,
    )
    event = EmailEvent.objects.create(
        contact=contact,
        client=client,
        audience=audience,
        event_type=EmailEventType.UNSUBSCRIBE,
    )
    CmpCallback.objects.create(
        email_event=event,
        contact=contact,
        client=client,
        audience=audience,
        event_id="datamailer-email-event:1",
        event_type="subscription.unsubscribed",
        callback_url="https://cmp.example/hooks/datamailer",
        payload={"email": contact.email},
        status=CmpCallbackStatus.PENDING,
        next_attempt_at=timezone.now(),
    )
