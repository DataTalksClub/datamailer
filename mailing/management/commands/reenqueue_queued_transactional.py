"""Re-enqueue transactional messages stuck in the ``queued`` state.

Transactional sends create messages as ``queued`` and push them to SQS on
commit; ``process_sqs_worker`` then sends them. If the worker is down, or the
on-commit enqueue failed, messages stay ``queued`` and are never delivered.

This command finds such messages and re-enqueues them to SQS (idempotently --
the worker claims each message atomically and skips anything already
sending/sent, so re-enqueuing a message that is still in SQS is safe).

    # Count what's stuck (also answers "how many didn't receive it"):
    python manage.py reenqueue_queued_transactional --dry-run

    # Drain everything queued for at least 5 minutes:
    python manage.py reenqueue_queued_transactional --older-than-minutes 5

    # Scope to one client / template / list:
    python manage.py reenqueue_queued_transactional --client dtc-courses \
        --template-key homework-score-notification
"""

from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from mailing.models import TransactionalMessage, TransactionalMessageStatus
from mailing.services.transactional import build_transactional_queue_payload
from mailing.sqs import enqueue_transactional_email


class Command(BaseCommand):
    help = "Re-enqueue transactional messages stuck in the queued state."

    def add_arguments(self, parser):
        parser.add_argument(
            "--older-than-minutes",
            type=int,
            default=0,
            help="Only messages queued at least this many minutes ago.",
        )
        parser.add_argument(
            "--client",
            default="",
            help="Limit to a client slug (default: all clients).",
        )
        parser.add_argument(
            "--template-key",
            default="",
            help="Limit to a template key (default: all templates).",
        )
        parser.add_argument(
            "--list-key",
            default="",
            help="Limit to a recipient list key (matches message metadata).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Cap the number of messages processed (0 = no cap).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report counts only; do not enqueue.",
        )

    def handle(self, *args, **options):
        qs = TransactionalMessage.objects.filter(
            status=TransactionalMessageStatus.QUEUED
        ).select_related("client")

        minutes = options["older_than_minutes"]
        if minutes > 0:
            qs = qs.filter(created_at__lte=timezone.now() - timedelta(minutes=minutes))
        if options["client"]:
            qs = qs.filter(client__slug=options["client"])
        if options["template_key"]:
            qs = qs.filter(template_key=options["template_key"])
        if options["list_key"]:
            qs = qs.filter(metadata__recipient_list_key=options["list_key"])

        qs = qs.order_by("created_at", "id")
        total = qs.count()

        # Breakdown by template so the dry-run doubles as a "stuck queue" report.
        by_template = Counter(qs.values_list("template_key", flat=True))

        self.stdout.write(f"queued messages matching filters: {total}")
        for key, count in sorted(by_template.items()):
            self.stdout.write(f"  {key}: {count}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("dry-run: nothing enqueued."))
            return

        limit = options["limit"]
        processed = 0
        failed = 0
        for message in qs.iterator():
            if limit and processed >= limit:
                break
            try:
                enqueue_transactional_email(build_transactional_queue_payload(message))
                processed += 1
            except Exception:
                failed += 1
                self.stderr.write(
                    f"failed to enqueue transactional message {message.id}"
                )

        self.stdout.write(
            self.style.SUCCESS(f"re-enqueued {processed} message(s); {failed} failed.")
        )
