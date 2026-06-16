from django.core.management.base import BaseCommand, CommandError

from mailing.forms import ClientForm
from mailing.models import Client


class Command(BaseCommand):
    help = "Configure the default and allowed transactional sender addresses for a client."

    def add_arguments(self, parser):
        parser.add_argument("client_slug")
        parser.add_argument("--organization", default="", help="Organization slug. Required when client slug is ambiguous.")
        parser.add_argument("--default-from", default="", help="Default sender used when API payload from_email is omitted.")
        parser.add_argument(
            "--allow",
            action="append",
            default=[],
            help="Allowed explicit sender. Can be passed multiple times.",
        )

    def handle(self, *args, **options):
        queryset = Client.objects.select_related("organization").filter(slug=options["client_slug"])
        if options["organization"]:
            queryset = queryset.filter(organization__slug=options["organization"])

        clients = list(queryset)
        if not clients and options["organization"]:
            clients = list(Client.objects.select_related("organization").filter(slug=options["client_slug"]))
        if not clients:
            self.stdout.write(self.style.WARNING("Client not found; sender configuration was skipped."))
            return
        if len(clients) > 1:
            raise CommandError("Client slug is ambiguous. Pass --organization.")

        client = clients[0]
        data = {
            "organization": client.organization_id,
            "name": client.name,
            "slug": client.slug,
            "default_from_email": options["default_from"],
            "allowed_from_emails": "\n".join(options["allow"]),
            "is_active": client.is_active,
        }
        form = ClientForm(data=data, instance=client)
        if not form.is_valid():
            raise CommandError(form.errors.as_json())

        client.default_from_email = form.cleaned_data["default_from_email"]
        client.allowed_from_emails = form.cleaned_data["allowed_from_emails"]
        client.save(update_fields=["default_from_email", "allowed_from_emails", "updated_at"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Configured senders for {client.organization.slug}/{client.slug}: "
                f"default={client.default_from_email or '-'}"
            )
        )
