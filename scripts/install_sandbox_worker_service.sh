#!/usr/bin/env bash
set -euo pipefail

install_worker_service() {
  local worker_name="$1"
  local command_name="$2"
  local description="$3"

  cat >"/etc/systemd/system/datamailer-${worker_name}-worker.service" <<SERVICE
[Unit]
Description=${description}
After=network-online.target datamailer.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/datamailer
EnvironmentFile=/opt/datamailer/.env
ExecStart=/opt/datamailer/.venv/bin/python manage.py process_sqs_worker ${command_name} --batch-size 10 --wait-time 20
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SERVICE
}

install_worker_service transactional transactional "Datamailer sandbox transactional SQS worker"
install_worker_service ses-webhooks ses-webhooks "Datamailer sandbox SES webhook SQS worker"

systemctl daemon-reload
systemctl enable datamailer-transactional-worker
systemctl enable datamailer-ses-webhooks-worker
systemctl restart datamailer-transactional-worker
systemctl restart datamailer-ses-webhooks-worker
systemctl is-active datamailer-transactional-worker
systemctl is-active datamailer-ses-webhooks-worker
