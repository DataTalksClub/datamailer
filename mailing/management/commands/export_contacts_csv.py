from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from mailing.models import Client
from mailing.services.contact_import_export import export_contacts_csv_for_client


class Command(BaseCommand):
    help = "Export Datamailer contacts for one authenticated client/audience scope as safe CSV."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="Organization slug.")
        parser.add_argument("--audience", required=True, help="Audience slug within the organization.")
        parser.add_argument("--client", required=True, help="Client slug within the organization.")
        parser.add_argument("--output", required=True, help="CSV output path.")
        parser.add_argument("--tags", default="", help="Comma-separated tag slugs to require.")
        parser.add_argument("--subscription-status", default="", help="pending, subscribed, or unsubscribed.")
        parser.add_argument("--verified", default="", help="Optional true/false filter.")
        parser.add_argument("--email-validation-status", default="", help="Optional email validation status filter.")
        parser.add_argument("--suppression", default="", help="none, any, global_unsubscribed, hard_bounced, or complained.")
        parser.add_argument("--updated-since", default="", help="Optional ISO datetime lower bound for contact updates.")
        parser.add_argument("--limit", default="10000", help="Maximum rows to export, up to 10000.")

    def handle(self, *args, **options):
        client = Client.objects.filter(
            organization__slug=options["organization"],
            slug=options["client"],
        ).first()
        if client is None:
            raise CommandError(
                f"Client '{options['client']}' was not found in organization '{options['organization']}'."
            )

        data = {
            "audience": options["audience"],
            "client": options["client"],
            "tags": options["tags"],
            "subscription_status": options["subscription_status"],
            "verified": options["verified"],
            "email_validation_status": options["email_validation_status"],
            "suppression": options["suppression"],
            "updated_since": options["updated_since"],
            "limit": options["limit"],
        }
        try:
            csv_body = export_contacts_csv_for_client(data, client)
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        output_path = Path(options["output"])
        output_path.write_text(csv_body, encoding="utf-8")
        self.stdout.write(f"Exported contacts to {output_path}")
