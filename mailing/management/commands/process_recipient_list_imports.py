from django.core.management.base import BaseCommand

from mailing.services.recipient_lists import process_pending_recipient_list_import_jobs


class Command(BaseCommand):
    help = "Process pending recipient-list import jobs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=25,
            help="Maximum number of pending import jobs to process.",
        )

    def handle(self, *args, **options):
        result = process_pending_recipient_list_import_jobs(limit=options["limit"])
        self.stdout.write(
            "processed={processed} succeeded={succeeded} failed={failed}".format(**result)
        )
