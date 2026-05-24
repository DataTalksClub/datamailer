from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from mailing.services.audience_import import (
    SUPPORTED_COLUMNS,
    AudienceImportError,
    ImportTarget,
    import_audience_csv,
)


class Command(BaseCommand):
    help = (
        "Import audience contacts from CSV. Required CSV column: email. Supported optional columns: "
        f"{', '.join(column for column in SUPPORTED_COLUMNS if column != 'email')}. "
        "Boolean columns accept true/false, yes/no, 1/0. Tags use semicolon-separated values. "
        "Duplicate emails use first valid row wins. Dry-run emits the same JSON report without database writes."
    )

    def add_arguments(self, parser):
        parser.add_argument("--csv", required=True, help="Path to the CSV file to import.")
        parser.add_argument("--organization", required=True, help="Target organization slug.")
        parser.add_argument("--audience", required=True, help="Target audience slug within the organization.")
        parser.add_argument(
            "--client",
            default=None,
            help="Optional target client slug within the organization. Omit for audience-only subscriptions.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and report what would be imported without creating or updating database records.",
        )
        parser.add_argument(
            "--report",
            default=None,
            help="Optional path for the JSON validation/import report. Defaults to stdout.",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv"])
        if not csv_path.exists():
            raise CommandError(f"CSV path does not exist: {csv_path}")
        if not csv_path.is_file():
            raise CommandError(f"CSV path is not a file: {csv_path}")

        target = ImportTarget(
            organization_slug=options["organization"],
            audience_slug=options["audience"],
            client_slug=options["client"],
        )
        try:
            report = import_audience_csv(csv_path, target, dry_run=options["dry_run"])
        except AudienceImportError as exc:
            raise CommandError(str(exc)) from exc

        report_json = report.to_json()
        if options["report"]:
            Path(options["report"]).write_text(report_json + "\n", encoding="utf-8")
        else:
            self.stdout.write(report_json)
