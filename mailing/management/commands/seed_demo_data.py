from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    CampaignRecipientSkipReason,
    CampaignRecipientStatus,
    CampaignStatus,
    Client,
    ClientApiKey,
    Contact,
    ContactTag,
    EmailEvent,
    EmailEventType,
    EmailTemplate,
    EmailValidationStatus,
    Organization,
    Subscription,
    SubscriptionStatus,
    Tag,
    TransactionalMessage,
    TransactionalMessageStatus,
)
from mailing.services.auth import hash_api_key

ADMIN_USERNAME = "admin"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin"

DEMO_API_KEYS = {
    "dtc-newsletter": {
        "name": "Newsletter import/export",
        "raw_key": "dm_dtcnews_demo_newsletter_import_export_key",
        "notes": "Stable local key for newsletter contact sync examples.",
    },
    "dtc-courses": {
        "name": "Course platform transactional",
        "raw_key": "dm_dtccourses_demo_transactional_email_key",
        "notes": "Stable local key for course registration and password email examples.",
    },
    "asl-platform": {
        "name": "ASL platform transactional",
        "raw_key": "dm_aslplatform_demo_transactional_email_key",
        "notes": "Stable local key for AI Shipping Labs transactional examples.",
    },
}


class Command(BaseCommand):
    help = (
        "Seed local-only Datamailer demo data for manual operator UI testing. "
        "Creates admin/admin, demo organizations, clients, contacts, campaigns, "
        "transactional messages, and engagement/suppression events without SQS or SES side effects."
    )

    def handle(self, *args, **options):
        if not settings.DEBUG and not getattr(settings, "TESTING", False):
            raise CommandError("seed_demo_data is local-only and requires DEBUG=True.")

        with transaction.atomic():
            seed_demo_data()

        self.stdout.write(self.style.SUCCESS("Seeded local demo data."))
        self.stdout.write(f"Admin login: {ADMIN_USERNAME} / {ADMIN_PASSWORD}")
        self.stdout.write("Demo API keys:")
        for client_slug, key_spec in DEMO_API_KEYS.items():
            self.stdout.write(f"- {client_slug} / {key_spec['name']}: {key_spec['raw_key']}")


def seed_demo_data():
    now = timezone.now()
    admin = upsert_admin_user()
    organizations = upsert_organizations()
    audiences = upsert_audiences(organizations)
    clients = upsert_clients(organizations)
    upsert_client_api_keys(clients)
    tags = upsert_tags(audiences)
    contacts = upsert_contacts(now)
    upsert_subscriptions(contacts, audiences, clients, now)
    upsert_contact_tags(contacts, tags)
    templates = upsert_templates(clients)
    campaigns = upsert_campaigns(audiences, clients, now)
    recipients = upsert_campaign_recipients(campaigns, contacts, now)
    messages = upsert_transactional_messages(clients, contacts, templates, now)
    upsert_campaign_events(recipients)
    upsert_transactional_events(messages)
    refresh_campaign_counts(campaigns.values())
    return {
        "admin": admin,
        "organizations": organizations,
        "audiences": audiences,
        "clients": clients,
        "tags": tags,
        "contacts": contacts,
        "templates": templates,
        "campaigns": campaigns,
        "recipients": recipients,
        "messages": messages,
    }


def upsert_admin_user():
    User = get_user_model()
    user, _ = User.objects.update_or_create(
        username=ADMIN_USERNAME,
        defaults={
            "email": ADMIN_EMAIL,
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
        },
    )
    user.set_password(ADMIN_PASSWORD)
    user.save(update_fields=["password", "email", "is_staff", "is_superuser", "is_active"])
    return user


def upsert_organizations():
    return {
        "datatalksclub": upsert_model(
            Organization,
            {"slug": "datatalksclub"},
            {"name": "DataTalksClub"},
        ),
        "ai-shipping-labs": upsert_model(
            Organization,
            {"slug": "ai-shipping-labs"},
            {"name": "AI Shipping Labs"},
        ),
    }


def upsert_audiences(organizations):
    return {
        "datatalks-club": upsert_model(
            Audience,
            {"organization": organizations["datatalksclub"], "slug": "datatalks-club"},
            {"name": "DataTalksClub Newsletter"},
        ),
        "dtc-courses": upsert_model(
            Audience,
            {"organization": organizations["datatalksclub"], "slug": "dtc-courses"},
            {"name": "DataTalksClub Courses"},
        ),
        "ai-shipping-labs": upsert_model(
            Audience,
            {"organization": organizations["ai-shipping-labs"], "slug": "ai-shipping-labs"},
            {"name": "AI Shipping Labs"},
        ),
    }


def upsert_clients(organizations):
    clients = {
        "dtc-newsletter": (
            "DataTalksClub Newsletter",
            organizations["datatalksclub"],
            "newsletter",
            "newsletter@dtcdev.click",
            "newsletter@dtcdev.click",
        ),
        "dtc-courses": (
            "DTC Courses",
            organizations["datatalksclub"],
            "courses",
            "courses@dtcdev.click",
            "DataTalks.Club Courses <courses@dtcdev.click>",
        ),
        "asl-platform": (
            "AI Shipping Labs Platform",
            organizations["ai-shipping-labs"],
            "hello",
            "hello@dtcdev.click",
            "hello@dtcdev.click",
        ),
    }
    return {
        slug: upsert_model(
            Client,
            {"organization": organization, "slug": slug},
            {
                "name": name,
                "default_from_email": default_from_email,
                "allowed_from_emails": [default_from_email],
                "default_sender_id": sender_id,
                "sender_emails": [{"id": sender_id, "email": sender_email}],
                "is_active": True,
            },
        )
        for slug, (
            name,
            organization,
            sender_id,
            default_from_email,
            sender_email,
        ) in clients.items()
    }


def upsert_client_api_keys(clients):
    for slug, client in clients.items():
        key_spec = DEMO_API_KEYS[slug]
        public_id = key_spec["raw_key"].removeprefix("dm_").split("_", 1)[0]
        api_key, _created = ClientApiKey.objects.update_or_create(
            client=client,
            name=key_spec["name"],
            defaults={
                "public_id": public_id,
                "key_hash": hash_api_key(key_spec["raw_key"]),
                "notes": key_spec["notes"],
                "revoked_at": None,
            },
        )
        if api_key.revoked_at:
            api_key.revoked_at = None
            api_key.save(update_fields=["revoked_at", "updated_at"])


def upsert_tags(audiences):
    tag_specs = [
        ("datatalks-club", "newsletter", "Newsletter"),
        ("datatalks-club", "ml-zoomcamp", "ML Zoomcamp"),
        ("datatalks-club", "data-engineering", "Data Engineering"),
        ("datatalks-club", "events", "Events"),
        ("datatalks-club", "inactive", "Inactive"),
        ("dtc-courses", "course-ml-zoomcamp", "Course: ML Zoomcamp"),
        ("dtc-courses", "course-de-zoomcamp", "Course: DE Zoomcamp"),
        ("ai-shipping-labs", "founder", "Founder"),
        ("ai-shipping-labs", "workshop", "Workshop"),
    ]
    return {
        slug: upsert_model(
            Tag,
            {"audience": audiences[audience_slug], "slug": slug},
            {"name": name},
        )
        for audience_slug, slug, name in tag_specs
    }


def upsert_contacts(now):
    contact_specs = [
        ("alex.verified@example.com", {"verified_at": now, "email_validation_status": EmailValidationStatus.VALID}),
        ("bailey.unverified@example.com", {"verified_at": None, "email_validation_status": EmailValidationStatus.UNKNOWN}),
        (
            "casey.global-unsub@example.com",
            {"verified_at": now, "global_unsubscribed_at": now, "email_validation_status": EmailValidationStatus.VALID},
        ),
        (
            "drew.hard-bounce@example.com",
            {"verified_at": now, "hard_bounced_at": now, "email_validation_status": EmailValidationStatus.NO_MX},
        ),
        (
            "erin.complaint@example.com",
            {"verified_at": now, "complained_at": now, "email_validation_status": EmailValidationStatus.RISKY},
        ),
        ("fatima.client-unsub@example.com", {"verified_at": now, "email_validation_status": EmailValidationStatus.VALID}),
        ("gabe.audience-unsub@example.com", {"verified_at": now, "email_validation_status": EmailValidationStatus.VALID}),
        (
            "harper.multi@example.com",
            {"verified_at": now, "email_validation_status": EmailValidationStatus.EXTERNALLY_VALIDATED},
        ),
        ("ivy.suppressed@example.com", {"verified_at": now, "email_validation_status": EmailValidationStatus.UNKNOWN}),
        ("jules.clicked@example.com", {"verified_at": now, "email_validation_status": EmailValidationStatus.VALID}),
        ("kai.pending@example.com", {"verified_at": now, "email_validation_status": EmailValidationStatus.UNKNOWN}),
        (
            "lina.failed@example.com",
            {
                "verified_at": now,
                "email_validation_status": EmailValidationStatus.MANUALLY_INVALID,
                "email_validation_reason": "demo manual hygiene review",
            },
        ),
    ]
    return {
        email: upsert_model(
            Contact,
            {"normalized_email": email},
            {
                "email": email,
                "email_validation_reason": "",
                "email_validated_at": now
                if fields["email_validation_status"] != EmailValidationStatus.UNKNOWN
                else None,
                "global_unsubscribed_at": None,
                "hard_bounced_at": None,
                "complained_at": None,
            }
            | fields,
        )
        for email, fields in contact_specs
    }


def upsert_subscriptions(contacts, audiences, clients, now):
    specs = [
        ("alex.verified@example.com", "datatalks-club", "dtc-newsletter", SubscriptionStatus.SUBSCRIBED),
        ("alex.verified@example.com", "dtc-courses", "dtc-courses", SubscriptionStatus.SUBSCRIBED),
        ("bailey.unverified@example.com", "datatalks-club", "dtc-newsletter", SubscriptionStatus.SUBSCRIBED),
        ("casey.global-unsub@example.com", "datatalks-club", "dtc-newsletter", SubscriptionStatus.SUBSCRIBED),
        ("drew.hard-bounce@example.com", "datatalks-club", "dtc-newsletter", SubscriptionStatus.SUBSCRIBED),
        ("erin.complaint@example.com", "datatalks-club", "dtc-newsletter", SubscriptionStatus.SUBSCRIBED),
        ("fatima.client-unsub@example.com", "datatalks-club", "dtc-newsletter", SubscriptionStatus.UNSUBSCRIBED),
        ("gabe.audience-unsub@example.com", "datatalks-club", None, SubscriptionStatus.UNSUBSCRIBED),
        ("gabe.audience-unsub@example.com", "datatalks-club", "dtc-newsletter", SubscriptionStatus.SUBSCRIBED),
        ("harper.multi@example.com", "datatalks-club", "dtc-newsletter", SubscriptionStatus.SUBSCRIBED),
        ("harper.multi@example.com", "dtc-courses", "dtc-courses", SubscriptionStatus.SUBSCRIBED),
        ("harper.multi@example.com", "ai-shipping-labs", "asl-platform", SubscriptionStatus.SUBSCRIBED),
        ("ivy.suppressed@example.com", "datatalks-club", "dtc-newsletter", SubscriptionStatus.PENDING),
        ("jules.clicked@example.com", "datatalks-club", "dtc-newsletter", SubscriptionStatus.SUBSCRIBED),
        ("kai.pending@example.com", "datatalks-club", "dtc-newsletter", SubscriptionStatus.SUBSCRIBED),
        ("lina.failed@example.com", "datatalks-club", "dtc-newsletter", SubscriptionStatus.SUBSCRIBED),
    ]
    for email, audience_slug, client_slug, status in specs:
        unsubscribed_at = now if status == SubscriptionStatus.UNSUBSCRIBED else None
        upsert_model(
            Subscription,
            {
                "contact": contacts[email],
                "audience": audiences[audience_slug],
                "client": clients[client_slug] if client_slug else None,
            },
            {
                "status": status,
                "verified_at": now if status == SubscriptionStatus.SUBSCRIBED else None,
                "unsubscribed_at": unsubscribed_at,
                "unsubscribe_reason": "demo suppression" if unsubscribed_at else "",
            },
        )


def upsert_contact_tags(contacts, tags):
    specs = [
        ("alex.verified@example.com", "newsletter"),
        ("alex.verified@example.com", "ml-zoomcamp"),
        ("alex.verified@example.com", "course-ml-zoomcamp"),
        ("harper.multi@example.com", "newsletter"),
        ("harper.multi@example.com", "events"),
        ("harper.multi@example.com", "founder"),
        ("jules.clicked@example.com", "newsletter"),
        ("jules.clicked@example.com", "data-engineering"),
        ("ivy.suppressed@example.com", "inactive"),
    ]
    for email, tag_slug in specs:
        ContactTag.objects.get_or_create(contact=contacts[email], tag=tags[tag_slug])


def upsert_templates(clients):
    specs = [
        (
            "dtc-courses",
            "email-verification",
            "Email Verification",
            "Verify a course account email after the client app creates its verification URL.",
            "Verify your DataTalksClub email",
            "<p>Hello {{ name }}, confirm your email: <a href=\"{{ verification_url }}\">Verify email</a>.</p>",
            "Hello {{ name }}, confirm your email: {{ verification_url }}",
            [
                {"name": "name", "description": "Recipient display name."},
                {"name": "verification_url", "description": "Client-generated verification URL."},
            ],
            {"name": "Alex", "verification_url": "https://client.example/verify/placeholder"},
        ),
        (
            "dtc-courses",
            "password-reset",
            "Password Reset",
            "Send a client-generated password reset URL.",
            "Reset your DataTalksClub password",
            "<p>Use this reset link: {{ reset_url }}</p>",
            "Use this reset link: {{ reset_url }}",
            [{"name": "reset_url", "description": "Client-generated password reset URL."}],
            {"reset_url": "https://client.example/reset/placeholder"},
        ),
        (
            "dtc-courses",
            "registration-welcome",
            "Registration Welcome",
            "Welcome a learner after registration.",
            "Welcome to DataTalksClub",
            "<p>Hello {{ name }}, your {{ course_name }} registration is confirmed.</p>",
            "Hello {{ name }}, your {{ course_name }} registration is confirmed.",
            [
                {"name": "name", "description": "Recipient display name."},
                {"name": "course_name", "description": "Course or program name."},
            ],
            {"name": "Alex", "course_name": "ML Zoomcamp"},
        ),
        (
            "asl-platform",
            "workshop-reminder",
            "Workshop Reminder",
            "Remind AI Shipping Labs users about an upcoming workshop.",
            "Your AI Shipping Labs workshop starts soon",
            "<p>Your workshop starts at {{ starts_at }}.</p>",
            "Your workshop starts at {{ starts_at }}.",
            [{"name": "starts_at", "description": "Human-readable workshop start time."}],
            {"starts_at": "2026-06-01 16:00 UTC"},
        ),
    ]
    return {
        key: upsert_model(
            EmailTemplate,
            {"client": clients[client_slug], "key": key},
            {
                "name": name,
                "subject": subject,
                "html_body": html_body,
                "text_body": text_body,
                "description": description,
                "required_context": required_context,
                "example_context": example_context,
                "is_transactional": True,
                "is_active": True,
            },
        )
        for client_slug, key, name, description, subject, html_body, text_body, required_context, example_context in specs
    }


def upsert_campaigns(audiences, clients, now):
    specs = [
        (
            "demo-draft",
            "Demo Draft: May Newsletter",
            CampaignStatus.DRAFT,
            {"include_tags": ["newsletter"], "exclude_tags": ["inactive"]},
        ),
        (
            "demo-queued",
            "Demo Queued: Course Launch",
            CampaignStatus.QUEUED,
            {"scheduled_at": now, "include_tags": ["ml-zoomcamp"]},
        ),
        (
            "demo-sending",
            "Demo Sending: Event Reminder",
            CampaignStatus.SENDING,
            {"scheduled_at": now, "include_tags": ["events"]},
        ),
        (
            "demo-sent",
            "Demo Sent: Weekly Roundup",
            CampaignStatus.SENT,
            {"scheduled_at": now, "sent_at": now, "include_tags": ["newsletter"]},
        ),
    ]
    return {
        key: upsert_model(
            Campaign,
            {
                "client": clients["dtc-newsletter"],
                "audience": audiences["datatalks-club"],
                "subject": subject,
            },
            {
                "preview_text": "Demo data for local operator UI testing.",
                "html_body": "<h1>Datamailer demo campaign</h1><p>Seeded local content.</p>",
                "text_body": "Datamailer demo campaign\n\nSeeded local content.",
                "status": status,
                "scheduled_at": fields.get("scheduled_at"),
                "sent_at": fields.get("sent_at"),
                "include_tags": fields.get("include_tags", []),
                "exclude_tags": fields.get("exclude_tags", []),
            },
        )
        for key, subject, status, fields in specs
    }


def upsert_campaign_recipients(campaigns, contacts, now):
    sent_campaign = campaigns["demo-sent"]
    specs = [
        ("demo-queued", "kai.pending@example.com", CampaignRecipientStatus.PENDING, "", {}),
        (
            "demo-queued",
            "bailey.unverified@example.com",
            CampaignRecipientStatus.SKIPPED,
            CampaignRecipientSkipReason.UNVERIFIED,
            {},
        ),
        ("demo-sending", "alex.verified@example.com", CampaignRecipientStatus.SENT, "", {"sent_at": now}),
        ("demo-sending", "lina.failed@example.com", CampaignRecipientStatus.FAILED, "", {"last_error": "Demo SMTP timeout"}),
        (
            "demo-sent",
            "alex.verified@example.com",
            CampaignRecipientStatus.SENT,
            "",
            {"sent_at": now, "delivered_at": now, "ses_message_id": "demo-campaign-sent-alex"},
        ),
        (
            "demo-sent",
            "harper.multi@example.com",
            CampaignRecipientStatus.SENT,
            "",
            {"sent_at": now, "delivered_at": now, "ses_message_id": "demo-campaign-sent-harper"},
        ),
        (
            "demo-sent",
            "jules.clicked@example.com",
            CampaignRecipientStatus.SENT,
            "",
            {
                "sent_at": now,
                "delivered_at": now,
                "first_opened_at": now,
                "first_clicked_at": now,
                "open_count": 2,
                "click_count": 1,
                "ses_message_id": "demo-campaign-sent-jules",
            },
        ),
        (
            "demo-sent",
            "fatima.client-unsub@example.com",
            CampaignRecipientStatus.UNSUBSCRIBED,
            "",
            {"sent_at": now, "delivered_at": now},
        ),
        (
            "demo-sent",
            "drew.hard-bounce@example.com",
            CampaignRecipientStatus.BOUNCED,
            "",
            {"sent_at": now, "last_error": "Permanent bounce"},
        ),
        (
            "demo-sent",
            "erin.complaint@example.com",
            CampaignRecipientStatus.COMPLAINED,
            "",
            {"sent_at": now, "last_error": "Spam complaint"},
        ),
        (
            "demo-sent",
            "casey.global-unsub@example.com",
            CampaignRecipientStatus.SKIPPED,
            CampaignRecipientSkipReason.GLOBAL_UNSUBSCRIBE,
            {},
        ),
        ("demo-sent", "lina.failed@example.com", CampaignRecipientStatus.FAILED, "", {"last_error": "SES rejected demo"}),
    ]
    recipients = {}
    for campaign_key, email, status, skip_reason, fields in specs:
        campaign = campaigns[campaign_key]
        contact = contacts[email]
        recipient = upsert_model(
            CampaignRecipient,
            {"campaign": campaign, "contact": contact},
            {
                "email": contact.email,
                "status": status,
                "skip_reason": skip_reason,
                "tracking_token_hash": f"demo-tracking-{campaign_key}-{contact.normalized_email}",
                "unsubscribe_token_hash": f"demo-unsubscribe-{campaign_key}-{contact.normalized_email}",
                "ses_message_id": "",
                "sent_at": None,
                "delivered_at": None,
                "first_opened_at": None,
                "first_clicked_at": None,
                "open_count": 0,
                "click_count": 0,
                "last_error": "",
            }
            | fields,
        )
        recipients[(campaign_key, email)] = recipient
    recipients["sent_campaign"] = sent_campaign
    return recipients


def upsert_transactional_messages(clients, contacts, templates, now):
    specs = [
        (
            "queued",
            "dtc-courses",
            "alex.verified@example.com",
            "email-verification",
            TransactionalMessageStatus.QUEUED,
            {},
        ),
        (
            "sent",
            "dtc-courses",
            "harper.multi@example.com",
            "password-reset",
            TransactionalMessageStatus.SENT,
            {"sent_at": now, "delivered_at": now, "first_opened_at": now, "first_clicked_at": now, "open_count": 1, "click_count": 1},
        ),
        (
            "bounced",
            "dtc-courses",
            "drew.hard-bounce@example.com",
            "email-verification",
            TransactionalMessageStatus.BOUNCED,
            {"sent_at": now, "last_error": "Permanent bounce"},
        ),
        (
            "complained",
            "dtc-courses",
            "erin.complaint@example.com",
            "email-verification",
            TransactionalMessageStatus.COMPLAINED,
            {"sent_at": now, "last_error": "Spam complaint"},
        ),
        (
            "skipped",
            "dtc-courses",
            "drew.hard-bounce@example.com",
            "password-reset",
            TransactionalMessageStatus.SKIPPED,
            {"last_error": "hard_bounce"},
        ),
        (
            "failed",
            "asl-platform",
            "harper.multi@example.com",
            "workshop-reminder",
            TransactionalMessageStatus.FAILED,
            {"last_error": "Demo template rendering failure"},
        ),
    ]
    messages = {}
    for key, client_slug, email, template_key, status, fields in specs:
        client = clients[client_slug]
        contact = contacts[email]
        template = templates[template_key]
        message = upsert_model(
            TransactionalMessage,
            {"client": client, "idempotency_key": f"demo-{key}"},
            {
                "contact": contact,
                "email": contact.normalized_email,
                "from_email_id": client.default_sender_id,
                "from_email": client.default_from_email,
                "template": template,
                "template_key": template.key,
                "status": status,
                "subject": template.subject,
                "html_body": template.html_body,
                "text_body": template.text_body,
                "context": {
                    "name": contact.email.split("@")[0],
                    "verification_url": "https://client.example/verify/placeholder",
                    "reset_url": "https://client.example/reset/placeholder",
                    "starts_at": "tomorrow",
                    "course_name": "ML Zoomcamp",
                },
                "metadata": {"seed": "demo"},
                "ses_message_id": f"demo-transactional-{key}" if status != TransactionalMessageStatus.QUEUED else "",
                "sent_at": None,
                "delivered_at": None,
                "first_opened_at": None,
                "first_clicked_at": None,
                "open_count": 0,
                "click_count": 0,
                "last_error": "",
            }
            | fields,
        )
        messages[key] = message
    return messages


def upsert_campaign_events(recipients):
    event_specs = [
        ("demo-sent", "alex.verified@example.com", EmailEventType.SENT, "", {}),
        ("demo-sent", "alex.verified@example.com", EmailEventType.DELIVERED, "", {}),
        ("demo-sent", "jules.clicked@example.com", EmailEventType.SENT, "", {}),
        ("demo-sent", "jules.clicked@example.com", EmailEventType.DELIVERED, "", {}),
        ("demo-sent", "jules.clicked@example.com", EmailEventType.OPEN, "", {}),
        ("demo-sent", "jules.clicked@example.com", EmailEventType.CLICK, "https://datatalks.club/", {}),
        ("demo-sent", "fatima.client-unsub@example.com", EmailEventType.UNSUBSCRIBE, "", {"scope": "client"}),
        ("demo-sent", "drew.hard-bounce@example.com", EmailEventType.BOUNCE, "", {"bounce_type": "Permanent"}),
        ("demo-sent", "erin.complaint@example.com", EmailEventType.COMPLAINT, "", {"feedback_type": "abuse"}),
        ("demo-sent", "casey.global-unsub@example.com", EmailEventType.SKIPPED, "", {"reason": "global_unsubscribe"}),
        ("demo-sent", "lina.failed@example.com", EmailEventType.FAILED, "", {"reason": "ses_rejected"}),
        ("demo-queued", "kai.pending@example.com", EmailEventType.QUEUED, "", {}),
    ]
    for campaign_key, email, event_type, url, metadata in event_specs:
        recipient = recipients[(campaign_key, email)]
        upsert_event(
            provider_event_id=f"demo-campaign-{campaign_key}-{email}-{event_type}",
            event_type=event_type,
            campaign=recipient.campaign,
            campaign_recipient=recipient,
            contact=recipient.contact,
            client=recipient.campaign.client,
            audience=recipient.campaign.audience,
            url=url,
            metadata=metadata,
        )


def upsert_transactional_events(messages):
    event_specs = [
        ("queued", EmailEventType.QUEUED, {}),
        ("sent", EmailEventType.SENT, {}),
        ("sent", EmailEventType.DELIVERED, {}),
        ("sent", EmailEventType.OPEN, {}),
        ("sent", EmailEventType.CLICK, {"url": "https://datatalks.club/courses/"}),
        ("bounced", EmailEventType.BOUNCE, {"bounce_type": "Permanent"}),
        ("complained", EmailEventType.COMPLAINT, {"feedback_type": "abuse"}),
        ("skipped", EmailEventType.SKIPPED, {"reason": "hard_bounce"}),
        ("failed", EmailEventType.FAILED, {"reason": "template_rendering"}),
    ]
    for message_key, event_type, metadata in event_specs:
        message = messages[message_key]
        upsert_event(
            provider_event_id=f"demo-transactional-{message_key}-{event_type}",
            event_type=event_type,
            transactional_message=message,
            contact=message.contact,
            client=message.client,
            url=metadata.get("url", ""),
            metadata=metadata,
        )


def refresh_campaign_counts(campaigns):
    for campaign in campaigns:
        recipients = CampaignRecipient.objects.filter(campaign=campaign)
        campaign.recipient_count = recipients.exclude(status=CampaignRecipientStatus.SKIPPED).count()
        campaign.sent_count = recipients.filter(status=CampaignRecipientStatus.SENT).count()
        campaign.skipped_count = recipients.filter(status=CampaignRecipientStatus.SKIPPED).count()
        campaign.delivered_count = recipients.filter(delivered_at__isnull=False).count()
        campaign.unique_open_count = recipients.filter(first_opened_at__isnull=False).count()
        campaign.open_count = sum(recipients.values_list("open_count", flat=True))
        campaign.unique_click_count = recipients.filter(first_clicked_at__isnull=False).count()
        campaign.click_count = sum(recipients.values_list("click_count", flat=True))
        campaign.unsubscribe_count = recipients.filter(status=CampaignRecipientStatus.UNSUBSCRIBED).count()
        campaign.bounce_count = recipients.filter(status=CampaignRecipientStatus.BOUNCED).count()
        campaign.complaint_count = recipients.filter(status=CampaignRecipientStatus.COMPLAINED).count()
        campaign.save(
            update_fields=[
                "recipient_count",
                "sent_count",
                "skipped_count",
                "delivered_count",
                "unique_open_count",
                "open_count",
                "unique_click_count",
                "click_count",
                "unsubscribe_count",
                "bounce_count",
                "complaint_count",
                "updated_at",
            ]
        )


def upsert_model(model, lookup, defaults):
    obj, _ = model.objects.update_or_create(**lookup, defaults=defaults)
    return obj


def upsert_event(**fields):
    provider_event_id = fields.pop("provider_event_id")
    return upsert_model(EmailEvent, {"provider_event_id": provider_event_id}, fields)
