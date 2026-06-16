from django.core.management.base import BaseCommand, CommandError

from mailing.forms import ClientForm
from mailing.models import Client


class Command(BaseCommand):
    help = "Configure named transactional sender addresses for a client."

    def add_arguments(self, parser):
        parser.add_argument("client_slug")
        parser.add_argument("--organization", default="", help="Organization slug. Required when client slug is ambiguous.")
        parser.add_argument("--default-sender", default="", help="Default sender ID used when API payload from_email is omitted.")
        parser.add_argument("--default-from", default="", help="Deprecated. Default sender email used to infer a sender ID.")
        parser.add_argument(
            "--sender",
            action="append",
            default=[],
            help="Configured sender as sender-id=email@example.com. Can be passed multiple times.",
        )
        parser.add_argument(
            "--allow",
            action="append",
            default=[],
            help="Deprecated. Sender email used to infer a sender ID. Can be passed multiple times.",
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
        sender_lines = list(options["sender"])
        if options["default_from"]:
            inferred_default = options["default_from"].split("@", 1)[0].replace(".", "-")
            sender_lines.append(f"{inferred_default}={options['default_from']}")
        for email in options["allow"]:
            inferred_id = email.split("@", 1)[0].replace(".", "-")
            sender_lines.append(f"{inferred_id}={email}")
        default_sender_id = options["default_sender"] or (
            options["default_from"].split("@", 1)[0].replace(".", "-") if options["default_from"] else ""
        )
        data = {
            "organization": client.organization_id,
            "name": client.name,
            "slug": client.slug,
            "default_sender_id": default_sender_id,
            "sender_emails": "\n".join(sender_lines),
            "is_active": client.is_active,
        }
        form = ClientForm(data=data, instance=client)
        if not form.is_valid():
            raise CommandError(form.errors.as_json())

        client.default_sender_id = form.cleaned_data["default_sender_id"]
        client.sender_emails = form.cleaned_data["sender_emails"]
        client.save(update_fields=["default_sender_id", "sender_emails", "updated_at"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Configured senders for {client.organization.slug}/{client.slug}: "
                f"default={client.default_sender_id or '-'}"
            )
        )
