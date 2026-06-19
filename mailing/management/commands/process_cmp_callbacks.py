from time import sleep

from django.core.management.base import BaseCommand

from mailing.services.cmp_callbacks import process_due_cmp_callbacks


class Command(BaseCommand):
    help = "Dispatch due CMP webhook callbacks from the Datamailer outbox."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process one batch and exit.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=25,
            help="Maximum callback rows to process per batch.",
        )
        parser.add_argument(
            "--idle-sleep",
            type=float,
            default=5.0,
            help="Seconds to sleep between empty batches in continuous mode.",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if batch_size < 1:
            self.stderr.write("batch-size must be at least 1.")
            return

        self.stdout.write("Starting CMP callback dispatcher")
        while True:
            result = process_due_cmp_callbacks(limit=batch_size)
            self.stdout.write("processed={processed} delivered={delivered} failed={failed}".format(**result))
            if options["once"]:
                return
            if result["processed"] == 0:
                sleep(options["idle_sleep"])
