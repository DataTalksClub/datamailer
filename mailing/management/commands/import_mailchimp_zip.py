from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from mailing.services.mailchimp_import import MailchimpImportError, MailchimpImportTarget, import_mailchimp_zip


class Command(BaseCommand):
    help = (
        "Import a local Mailchimp audience export zip. Reads subscribed, unsubscribed, and cleaned CSV files "
        "without extracting archive contents into the repo. Emits a public-safe JSON report."
    )

    def add_arguments(self, parser):
        parser.add_argument("--zip", required=True, help="Path to the Mailchimp audience export zip.")
        parser.add_argument("--organization", required=True, help="Target organization slug.")
        parser.add_argument("--audience", required=True, help="Target Datamailer audience slug.")
        parser.add_argument("--client", required=True, help="Target Datamailer client slug.")
        parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing data.")
        parser.add_argument("--report", default=None, help="Optional path for the JSON report. Defaults to stdout.")

    def handle(self, *args, **options):
        target = MailchimpImportTarget(
            organization_slug=options["organization"],
            audience_slug=options["audience"],
            client_slug=options["client"],
        )
        try:
            report = import_mailchimp_zip(options["zip"], target, dry_run=options["dry_run"])
        except MailchimpImportError as exc:
            raise CommandError(str(exc)) from exc

        report_json = report.to_json()
        if options["report"]:
            Path(options["report"]).write_text(report_json + "\n", encoding="utf-8")
        else:
            self.stdout.write(report_json)
