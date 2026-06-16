#!/usr/bin/env bash
set -euo pipefail

cat >/etc/systemd/system/datamailer-transactional-worker.service <<'SERVICE'
[Unit]
Description=Datamailer sandbox transactional SQS worker
After=network-online.target datamailer.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/datamailer
EnvironmentFile=/opt/datamailer/.env
ExecStart=/opt/datamailer/.venv/bin/python manage.py process_sqs_worker transactional --batch-size 10 --wait-time 20
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable datamailer-transactional-worker
systemctl restart datamailer-transactional-worker
systemctl is-active datamailer-transactional-worker
