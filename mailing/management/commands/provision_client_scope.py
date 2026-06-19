from django.core.management.base import BaseCommand, CommandError

from mailing.models import Audience, Client, Organization


class Command(BaseCommand):
    help = "Ensure an organization, audience, and client scope exists."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="Organization slug.")
        parser.add_argument("--organization-name", default="", help="Organization display name.")
        parser.add_argument("--audience", required=True, help="Audience slug.")
        parser.add_argument("--audience-name", default="", help="Audience display name.")
        parser.add_argument("--client", required=True, help="Client slug.")
        parser.add_argument("--client-name", default="", help="Client display name.")

    def handle(self, *args, **options):
        organization_slug = options["organization"].strip()
        audience_slug = options["audience"].strip()
        client_slug = options["client"].strip()
        if not organization_slug or not audience_slug or not client_slug:
            raise CommandError("organization, audience, and client slugs must be non-empty.")

        organization, _ = Organization.objects.update_or_create(
            slug=organization_slug,
            defaults={
                "name": options["organization_name"].strip() or organization_slug,
            },
        )
        audience, _ = Audience.objects.update_or_create(
            organization=organization,
            slug=audience_slug,
            defaults={
                "name": options["audience_name"].strip() or audience_slug,
            },
        )
        client, _ = Client.objects.update_or_create(
            organization=organization,
            slug=client_slug,
            defaults={
                "name": options["client_name"].strip() or client_slug,
                "is_active": True,
            },
        )

        existing_client_orgs = Organization.objects.filter(
            clients__slug=client_slug,
        ).exclude(pk=organization.pk)
        provisioned_extra_audiences = []
        for client_organization in existing_client_orgs.distinct():
            extra_audience, _ = Audience.objects.update_or_create(
                organization=client_organization,
                slug=audience_slug,
                defaults={
                    "name": options["audience_name"].strip() or audience_slug,
                },
            )
            provisioned_extra_audiences.append(f"{extra_audience.organization.slug}/{extra_audience.slug}")

        message = f"Provisioned scope organization={organization.slug} audience={audience.slug} client={client.slug}"
        if provisioned_extra_audiences:
            message = f"{message}; extra_audiences={','.join(provisioned_extra_audiences)}"
        self.stdout.write(self.style.SUCCESS(message))
