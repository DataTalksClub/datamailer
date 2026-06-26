from __future__ import annotations

import subprocess
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from mailing.models import (
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
    CmpCallback,
    CmpCallbackStatus,
    TransactionalMessage,
    TransactionalMessageStatus,
)


@dataclass(frozen=True)
class WorkerStatus:
    key: str
    label: str
    service_name: str
    command: str
    state: str
    detail: str
    badge_label: str
    badge_tone: str
    backlog_label: str
    backlog_count: int | None
    pid: str = ""
    started_at: str = ""
    restart_count: str = ""

    @property
    def alive(self) -> bool | None:
        if self.state == "active":
            return True
        if self.badge_tone == "danger":
            return False
        return None


@dataclass(frozen=True)
class WorkerDefinition:
    key: str
    label: str
    service_name: str
    command: str
    backlog_label: str


WORKER_DEFINITIONS = (
    WorkerDefinition(
        "transactional",
        "Transactional email",
        "datamailer-transactional-worker.service",
        "process_sqs_worker transactional",
        "Queued messages",
    ),
    WorkerDefinition(
        "campaign",
        "Campaign email",
        "datamailer-campaign-worker.service",
        "process_sqs_worker campaign",
        "Pending recipients",
    ),
    WorkerDefinition(
        "ses-webhooks",
        "SES webhooks",
        "datamailer-ses-webhooks-worker.service",
        "process_sqs_worker ses-webhooks",
        "SQS backlog",
    ),
    WorkerDefinition(
        "cmp-callbacks",
        "CMP callbacks",
        "datamailer-cmp-callbacks-worker.service",
        "process_cmp_callbacks",
        "Due callbacks",
    ),
)


def sandbox_worker_statuses() -> list[WorkerStatus]:
    return [_worker_status(definition) for definition in WORKER_DEFINITIONS]


def worker_status_payload() -> dict[str, object]:
    workers = sandbox_worker_statuses()
    return {
        "status": _overall_status(workers),
        "workers": [_serialize_worker_status(worker) for worker in workers],
    }


def systemd_service_properties(service_name: str) -> dict[str, str]:
    if not getattr(settings, "WORKER_STATUS_SYSTEMD_ENABLED", True):
        return {"ActiveState": "unknown", "UnavailableReason": "Systemd status checks are disabled."}

    try:
        completed = subprocess.run(
            [
                "systemctl",
                "show",
                service_name,
                "--property=LoadState,ActiveState,SubState,Result,MainPID,ExecMainStartTimestamp,NRestarts",
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=getattr(settings, "WORKER_STATUS_SYSTEMD_TIMEOUT_SECONDS", 1.5),
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        return {"ActiveState": "unknown", "UnavailableReason": str(exc)}

    properties = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    if completed.returncode != 0 and not properties:
        properties["ActiveState"] = "unknown"
        properties["UnavailableReason"] = (completed.stderr or "systemctl did not return service state.").strip()
    return properties


def _worker_status(definition: WorkerDefinition) -> WorkerStatus:
    systemd = systemd_service_properties(definition.service_name)
    badge_label, badge_tone, detail = _badge_for_systemd(systemd)
    return WorkerStatus(
        key=definition.key,
        label=definition.label,
        service_name=definition.service_name,
        command=definition.command,
        state=systemd.get("ActiveState", "unknown"),
        detail=detail,
        badge_label=badge_label,
        badge_tone=badge_tone,
        backlog_label=definition.backlog_label,
        backlog_count=_backlog_count(definition.key),
        pid=systemd.get("MainPID", ""),
        started_at=systemd.get("ExecMainStartTimestamp", ""),
        restart_count=systemd.get("NRestarts", ""),
    )


def _badge_for_systemd(properties: dict[str, str]) -> tuple[str, str, str]:
    load_state = properties.get("LoadState", "")
    active_state = properties.get("ActiveState", "unknown")
    sub_state = properties.get("SubState", "")
    result = properties.get("Result", "")

    if load_state == "not-found":
        return "Missing", "danger", "systemd unit is not installed"
    if active_state == "active":
        return "Running", "success", sub_state or "active"
    if active_state == "activating":
        return "Starting", "warning", sub_state or "activating"
    if active_state in {"failed", "inactive", "deactivating"}:
        detail = sub_state or active_state
        if result and result != "success":
            detail = f"{detail}; result={result}"
        return active_state.capitalize(), "danger", detail
    return "Unknown", "neutral", properties.get("UnavailableReason") or sub_state or active_state


def _backlog_count(worker_key: str) -> int | None:
    if worker_key == "transactional":
        return TransactionalMessage.objects.filter(status=TransactionalMessageStatus.QUEUED).count()
    if worker_key == "campaign":
        return CampaignRecipient.objects.filter(
            campaign__status__in=[CampaignStatus.QUEUED, CampaignStatus.SENDING],
            status=CampaignRecipientStatus.PENDING,
        ).count()
    if worker_key == "cmp-callbacks":
        return CmpCallback.objects.filter(
            Q(status=CmpCallbackStatus.PENDING, next_attempt_at__lte=timezone.now())
            | Q(status=CmpCallbackStatus.FAILED)
        ).count()
    return None


def _serialize_worker_status(worker: WorkerStatus) -> dict[str, object]:
    return {
        "key": worker.key,
        "label": worker.label,
        "service_name": worker.service_name,
        "command": worker.command,
        "alive": worker.alive,
        "state": worker.state,
        "status": worker.badge_label.lower(),
        "status_label": worker.badge_label,
        "status_tone": worker.badge_tone,
        "detail": worker.detail,
        "backlog": {
            "label": worker.backlog_label,
            "count": worker.backlog_count,
        },
        "runtime": {
            "pid": worker.pid,
            "started_at": worker.started_at,
            "restart_count": worker.restart_count,
        },
    }


def _overall_status(workers: list[WorkerStatus]) -> str:
    if any(worker.badge_tone == "danger" for worker in workers):
        return "degraded"
    if any(worker.alive is None for worker in workers):
        return "unknown"
    return "ok"
