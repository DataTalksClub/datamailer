from time import sleep

from django.core.management.base import BaseCommand

from mailing.services.recipient_lists import process_pending_recipient_list_import_jobs


class Command(BaseCommand):
    help = "Process pending recipient-list import jobs."

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
            help="Maximum number of pending import jobs to process.",
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

        self.stdout.write("Starting recipient-list import worker")
        while True:
            result = process_pending_recipient_list_import_jobs(limit=batch_size)
            self.stdout.write("processed={processed} succeeded={succeeded} failed={failed}".format(**result))
            if options["once"]:
                return
            if result["processed"] == 0:
                sleep(options["idle_sleep"])
