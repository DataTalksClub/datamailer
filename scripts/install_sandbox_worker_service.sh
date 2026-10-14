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
# Unbuffered so drain failures reach journald immediately instead of sitting in the stdout block buffer until the next restart.
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/datamailer/.venv/bin/python manage.py drain_sqs_ingress ${command_name} --batch-size 10 --wait-time 20
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
Environment=PYTHONUNBUFFERED=1
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
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/datamailer/.venv/bin/python manage.py process_recipient_list_imports --batch-size 10 --idle-sleep 5
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SERVICE
}

install_db_worker_service() {
  cat >"/etc/systemd/system/datamailer-db-worker.service" <<SERVICE
[Unit]
Description=Datamailer sandbox django.tasks worker
After=network-online.target datamailer.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/datamailer
EnvironmentFile=/opt/datamailer/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/datamailer/.venv/bin/python manage.py db_worker
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SERVICE
}

# Runs the django.tasks backend's worker. Without it every task enqueued
# through mailing/enqueue.py -- transactional sends, campaign batches, SES
# notifications arriving over HTTP -- is written to the database and never
# executed. The SQS worker units below do not cover it: they drain the AWS-fed
# queues, which is a different transport.
# Retired: transactional and campaign work is enqueued through django.tasks
# and drained by db_worker, so nothing writes to those two queues any more.
# Removed rather than merely not installed, so a host that already has them
# converges instead of running pollers against queues with no producer.
retire_worker_service() {
  local unit="datamailer-$1-worker"
  if systemctl list-unit-files "${unit}.service" >/dev/null 2>&1; then
    systemctl disable --now "$unit" 2>/dev/null || true
  fi
  rm -f "/etc/systemd/system/${unit}.service"
}

retire_worker_service transactional
retire_worker_service campaign

install_db_worker_service
# These two drain rather than process: each message becomes a task, so SES
# notifications and inbound mail run on the same execution path as everything
# else and appear in the status contract. Processing them inline -- which
# process_sqs_worker did -- left ~2,800 SES events a week invisible.
install_worker_service ses-webhooks ses-webhooks "Datamailer sandbox SES webhook ingress drain"
install_worker_service inbound-email inbound-email "Datamailer sandbox inbound email ingress drain"
install_cmp_callbacks_service
install_recipient_list_imports_service

systemctl daemon-reload
systemctl enable datamailer-db-worker
systemctl enable datamailer-ses-webhooks-worker
systemctl enable datamailer-inbound-email-worker
systemctl enable datamailer-cmp-callbacks-worker
systemctl enable datamailer-recipient-list-imports-worker
# One restart transaction for the whole worker set: one failing unit must
# not leave the others running the previous deploy's code.
systemctl restart datamailer-db-worker datamailer-ses-webhooks-worker datamailer-inbound-email-worker datamailer-cmp-callbacks-worker datamailer-recipient-list-imports-worker
systemctl is-active datamailer-db-worker
systemctl is-active datamailer-ses-webhooks-worker
systemctl is-active datamailer-inbound-email-worker
systemctl is-active datamailer-cmp-callbacks-worker
systemctl is-active datamailer-recipient-list-imports-worker
