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

install_cmp_callbacks_service() {
  cat >"/etc/systemd/system/datamailer-cmp-callbacks-worker.service" <<SERVICE
[Unit]
Description=Datamailer sandbox CMP callback dispatcher
After=network-online.target datamailer.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/datamailer
EnvironmentFile=/opt/datamailer/.env
ExecStart=/opt/datamailer/.venv/bin/python manage.py process_cmp_callbacks --batch-size 25 --idle-sleep 5
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SERVICE
}

install_recipient_list_imports_service() {
  cat >"/etc/systemd/system/datamailer-recipient-list-imports-worker.service" <<SERVICE
[Unit]
Description=Datamailer sandbox recipient-list import worker
After=network-online.target datamailer.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/datamailer
EnvironmentFile=/opt/datamailer/.env
ExecStart=/opt/datamailer/.venv/bin/python manage.py process_recipient_list_imports --batch-size 10 --idle-sleep 5
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SERVICE
}

install_worker_service transactional transactional "Datamailer sandbox transactional SQS worker"
install_worker_service campaign campaign "Datamailer sandbox campaign SQS worker"
install_worker_service ses-webhooks ses-webhooks "Datamailer sandbox SES webhook SQS worker"
install_cmp_callbacks_service
install_recipient_list_imports_service

systemctl daemon-reload
systemctl enable datamailer-transactional-worker
systemctl enable datamailer-campaign-worker
systemctl enable datamailer-ses-webhooks-worker
systemctl enable datamailer-cmp-callbacks-worker
systemctl enable datamailer-recipient-list-imports-worker
systemctl restart datamailer-transactional-worker
systemctl restart datamailer-campaign-worker
systemctl restart datamailer-ses-webhooks-worker
systemctl restart datamailer-cmp-callbacks-worker
systemctl restart datamailer-recipient-list-imports-worker
systemctl is-active datamailer-transactional-worker
systemctl is-active datamailer-campaign-worker
systemctl is-active datamailer-ses-webhooks-worker
systemctl is-active datamailer-cmp-callbacks-worker
systemctl is-active datamailer-recipient-list-imports-worker
