from io import StringIO

import pytest
from django.core.management import call_command

from mailing.models import (
    Client,
    Contact,
    EmailTemplate,
    Organization,
    TransactionalMessage,
    TransactionalMessageStatus,
)

ENQUEUE = (
    "mailing.management.commands.reenqueue_queued_transactional."
    "enqueue_transactional_email"
)


@pytest.fixture
def queued_message(db):
    org = Organization.objects.create(name="DataTalksClub", slug="datatalksclub")
    client = Client.objects.create(
        organization=org, name="DTC Courses", slug="dtc-courses"
    )
    contact = Contact.objects.create(email="person@example.com")
    template = EmailTemplate.objects.create(
        client=client,
        key="homework-score-notification",
        name="HW score",
        subject="s",
        html_body="<p>b</p>",
        text_body="b",
    )
    return TransactionalMessage.objects.create(
        client=client,
        contact=contact,
        email=contact.normalized_email,
        from_email_id="courses",
        from_email="courses@dtcdev.click",
        template=template,
        template_key=template.key,
        status=TransactionalMessageStatus.QUEUED,
        idempotency_key="hw-1",
        subject="s",
        html_body="<p>b</p>",
        text_body="b",
    )


def test_dry_run_counts_without_enqueue(queued_message, monkeypatch):
    calls = []
    monkeypatch.setattr(ENQUEUE, calls.append)
    out = StringIO()
    call_command("reenqueue_queued_transactional", "--dry-run", stdout=out)
    assert "queued messages matching filters: 1" in out.getvalue()
    assert "homework-score-notification: 1" in out.getvalue()
    assert calls == []


def test_reenqueues_queued_message(queued_message, monkeypatch):
    calls = []
    monkeypatch.setattr(ENQUEUE, calls.append)
    out = StringIO()
    call_command("reenqueue_queued_transactional", stdout=out)
    assert len(calls) == 1
    assert calls[0]["transactional_message_id"] == queued_message.id
    assert "re-enqueued 1" in out.getvalue()


def test_skips_already_sent(queued_message, monkeypatch):
    queued_message.status = TransactionalMessageStatus.SENT
    queued_message.save(update_fields=["status"])
    calls = []
    monkeypatch.setattr(ENQUEUE, calls.append)
    call_command("reenqueue_queued_transactional")
    assert calls == []


def test_template_filter_excludes_others(queued_message, monkeypatch):
    calls = []
    monkeypatch.setattr(ENQUEUE, calls.append)
    call_command(
        "reenqueue_queued_transactional", "--template-key", "other-key"
    )
    assert calls == []
