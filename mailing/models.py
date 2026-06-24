from django.db import models
from django.utils.text import slugify


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Organization(models.Model):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=120, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "organizations"
        ordering = ["slug"]

    def __str__(self):
        return self.name


class Audience(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="audiences")
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "audiences"
        ordering = ["organization__slug", "slug"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "slug"], name="unique_audience_slug_per_organization"),
        ]

    def __str__(self):
        return f"{self.name} ({self.organization.slug})"


class Client(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="clients")
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=120)
    default_from_email = models.EmailField(max_length=320, blank=True)
    allowed_from_emails = models.JSONField(default=list, blank=True)
    default_sender_id = models.SlugField(max_length=80, blank=True)
    sender_emails = models.JSONField(default=list, blank=True)
    cmp_webhook_url = models.URLField(max_length=2048, blank=True)
    cmp_webhook_token = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "clients"
        ordering = ["organization__slug", "slug"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "slug"], name="unique_client_slug_per_organization"),
        ]
        indexes = [
            models.Index(fields=["is_active"], name="clients_is_active_idx"),
        ]

    def __str__(self):
        return f"{self.name} ({self.organization.slug})"

    @property
    def active_api_key_count(self):
        return self.api_keys.filter(revoked_at__isnull=True).count()


class ClientApiKey(TimeStampedModel):
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=120)
    key_hash = models.CharField(max_length=255)
    public_id = models.CharField(max_length=32, unique=True)
    notes = models.TextField(blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "client_api_keys"
        ordering = ["client__organization__slug", "client__slug", "revoked_at", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "name"],
                condition=models.Q(revoked_at__isnull=True),
                name="unique_active_api_key_name_per_client",
            ),
        ]
        indexes = [
            models.Index(fields=["public_id"], name="client_api_keys_public_id_idx"),
            models.Index(fields=["client", "revoked_at"], name="client_keys_state_idx"),
        ]

    @property
    def is_active(self):
        return self.revoked_at is None

    @property
    def display_prefix(self):
        return f"dm_{self.public_id}"

    def __str__(self):
        return f"{self.client.slug} / {self.name}"


class EmailValidationStatus(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    VALID = "valid", "Valid"
    INVALID_SYNTAX = "invalid_syntax", "Invalid syntax"
    NO_MX = "no_mx", "No MX"
    DISPOSABLE = "disposable", "Disposable"
    RISKY = "risky", "Risky"
    MANUALLY_INVALID = "manually_invalid", "Manually invalid"
    EXTERNALLY_VALIDATED = "externally_validated", "Externally validated"


class Contact(TimeStampedModel):
    email = models.EmailField(max_length=320)
    normalized_email = models.EmailField(max_length=320, unique=True)
    verified_at = models.DateTimeField(null=True, blank=True, db_index=True)
    email_validation_status = models.CharField(
        max_length=30,
        choices=EmailValidationStatus.choices,
        default=EmailValidationStatus.UNKNOWN,
        db_index=True,
    )
    email_validation_reason = models.CharField(max_length=255, blank=True)
    email_validated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    global_unsubscribed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    hard_bounced_at = models.DateTimeField(null=True, blank=True)
    complained_at = models.DateTimeField(null=True, blank=True)
    tags = models.ManyToManyField("Tag", through="ContactTag", related_name="contacts")

    class Meta:
        db_table = "contacts"
        ordering = ["normalized_email"]
        indexes = [
            models.Index(fields=["hard_bounced_at"], name="contacts_hard_bounced_idx"),
            models.Index(fields=["complained_at"], name="contacts_complained_idx"),
        ]

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip()
            self.normalized_email = self.email.casefold()
        super().save(*args, **kwargs)


class SubscriptionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SUBSCRIBED = "subscribed", "Subscribed"
    UNSUBSCRIBED = "unsubscribed", "Unsubscribed"


class Subscription(TimeStampedModel):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="subscriptions")
    audience = models.ForeignKey(Audience, on_delete=models.CASCADE, related_name="subscriptions")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, null=True, blank=True, related_name="subscriptions")
    status = models.CharField(
        max_length=20,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.PENDING,
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    unsubscribe_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "subscriptions"
        ordering = ["audience__slug", "client__slug", "contact__normalized_email"]
        constraints = [
            models.UniqueConstraint(fields=["contact", "audience", "client"], name="unique_subscription_scope"),
            models.UniqueConstraint(
                fields=["contact", "audience"],
                condition=models.Q(client__isnull=True),
                name="unique_audience_only_subscription_scope",
            ),
        ]
        indexes = [
            models.Index(fields=["audience", "client", "status"], name="subs_aud_cli_status_idx"),
            models.Index(fields=["contact", "updated_at"], name="subs_contact_updated_idx"),
        ]

    def __str__(self):
        client_slug = self.client.slug if self.client_id else "audience"
        return f"{self.contact.normalized_email}: {self.audience.slug}/{client_slug} {self.status}"


class Tag(models.Model):
    audience = models.ForeignKey(Audience, on_delete=models.CASCADE, related_name="tags")
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, blank=True)

    class Meta:
        db_table = "tags"
        ordering = ["audience__slug", "slug"]
        constraints = [
            models.UniqueConstraint(fields=["audience", "slug"], name="unique_tag_slug_per_audience"),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.audience.slug})"


class ContactTag(models.Model):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="contact_tags")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="contact_tags")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contact_tags"
        ordering = ["tag__audience__slug", "tag__slug", "contact__normalized_email"]
        constraints = [
            models.UniqueConstraint(fields=["contact", "tag"], name="unique_contact_tag"),
        ]
        indexes = [
            models.Index(fields=["tag", "contact"], name="contact_tags_tag_contact_idx"),
        ]

    def __str__(self):
        return f"{self.contact.normalized_email}: {self.tag.slug}"


class ContactSourceMetadata(TimeStampedModel):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="source_metadata")
    audience = models.ForeignKey(Audience, on_delete=models.CASCADE, related_name="contact_source_metadata")
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="contact_source_metadata")
    source = models.CharField(max_length=80)
    external_id = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "contact_source_metadata"
        ordering = ["source", "contact__normalized_email"]
        constraints = [
            models.UniqueConstraint(
                fields=["contact", "audience", "client", "source"],
                name="unique_contact_source_metadata_scope",
            ),
        ]
        indexes = [
            models.Index(fields=["source", "external_id"], name="contact_src_ext_idx"),
            models.Index(fields=["audience", "client", "source"], name="contact_src_scope_idx"),
        ]

    def __str__(self):
        return f"{self.contact.normalized_email}: {self.source}"


class RecipientListType(models.TextChoices):
    REGISTRANTS = "registrants", "Registrants"
    HOMEWORK_SUBMITTERS = "homework_submitters", "Homework submitters"
    PROJECT_SUBMITTERS = "project_submitters", "Project submitters"
    CUSTOM = "custom", "Custom"


class RecipientList(TimeStampedModel):
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="recipient_lists")
    audience = models.ForeignKey(Audience, on_delete=models.CASCADE, related_name="recipient_lists")
    key = models.CharField(max_length=255)
    type = models.CharField(
        max_length=40,
        choices=RecipientListType.choices,
        default=RecipientListType.CUSTOM,
    )
    name = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict, blank=True)
    member_count = models.PositiveIntegerField(default=0)
    active_member_count = models.PositiveIntegerField(default=0)
    last_reconciled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "recipient_lists"
        ordering = ["client__slug", "audience__slug", "key"]
        constraints = [
            models.UniqueConstraint(fields=["client", "audience", "key"], name="unique_recipient_list_scope_key"),
        ]
        indexes = [
            models.Index(fields=["client", "audience", "type"], name="recip_list_scope_type_idx"),
        ]

    def __str__(self):
        return f"{self.client.slug}/{self.audience.slug}/{self.key}"


class RecipientListMember(TimeStampedModel):
    recipient_list = models.ForeignKey(RecipientList, on_delete=models.CASCADE, related_name="members")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="recipient_list_members")
    email = models.EmailField(max_length=320)
    source_object_key = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True, db_index=True)
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "recipient_list_members"
        ordering = ["recipient_list_id", "source_object_key"]
        constraints = [
            models.UniqueConstraint(fields=["recipient_list", "contact"], name="unique_recipient_list_contact"),
            models.UniqueConstraint(
                fields=["recipient_list", "source_object_key"],
                name="unique_recipient_list_source_object",
            ),
        ]
        indexes = [
            models.Index(fields=["recipient_list", "active"], name="recip_list_member_active_idx"),
        ]

    def __str__(self):
        return f"{self.recipient_list.key}: {self.source_object_key}"


class CampaignStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    QUEUED = "queued", "Queued"
    SNAPSHOTTING = "snapshotting", "Snapshotting"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"


def normalize_tag_filter(value):
    return sorted({slugify(str(tag).strip()) for tag in (value or []) if str(tag).strip()})


class Campaign(TimeStampedModel):
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="campaigns")
    audience = models.ForeignKey(Audience, on_delete=models.PROTECT, related_name="campaigns")
    subject = models.CharField(max_length=255)
    preview_text = models.CharField(max_length=255, blank=True)
    html_body = models.TextField(blank=True)
    text_body = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=CampaignStatus.choices,
        default=CampaignStatus.DRAFT,
    )
    scheduled_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    include_tags = models.JSONField(default=list, blank=True)
    exclude_tags = models.JSONField(default=list, blank=True)
    recipient_count = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    delivered_count = models.PositiveIntegerField(default=0)
    unique_open_count = models.PositiveIntegerField(default=0)
    open_count = models.PositiveIntegerField(default=0)
    unique_click_count = models.PositiveIntegerField(default=0)
    click_count = models.PositiveIntegerField(default=0)
    unsubscribe_count = models.PositiveIntegerField(default=0)
    bounce_count = models.PositiveIntegerField(default=0)
    complaint_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "campaigns"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["client", "audience", "status"], name="campaign_client_aud_status_idx"),
            models.Index(fields=["scheduled_at"], name="campaign_scheduled_at_idx"),
        ]

    def save(self, *args, **kwargs):
        self.include_tags = normalize_tag_filter(self.include_tags)
        self.exclude_tags = normalize_tag_filter(self.exclude_tags)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.subject


class CampaignRecipientStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    SKIPPED = "skipped", "Skipped"
    FAILED = "failed", "Failed"
    BOUNCED = "bounced", "Bounced"
    COMPLAINED = "complained", "Complained"
    UNSUBSCRIBED = "unsubscribed", "Unsubscribed"


class CampaignRecipientSkipReason(models.TextChoices):
    UNVERIFIED = "unverified", "Unverified"
    INVALID_EMAIL = "invalid_email", "Invalid email"
    GLOBAL_UNSUBSCRIBE = "global_unsubscribe", "Global unsubscribe"
    CLIENT_UNSUBSCRIBE = "client_unsubscribe", "Client unsubscribe"
    AUDIENCE_UNSUBSCRIBE = "audience_unsubscribe", "Audience unsubscribe"
    HARD_BOUNCE = "hard_bounce", "Hard bounce"
    COMPLAINT = "complaint", "Complaint"
    DUPLICATE = "duplicate", "Duplicate"
    SUPPRESSED = "suppressed", "Suppressed"


class CampaignRecipient(TimeStampedModel):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="campaign_recipients")
    email = models.EmailField(max_length=320)
    status = models.CharField(
        max_length=20,
        choices=CampaignRecipientStatus.choices,
        default=CampaignRecipientStatus.PENDING,
    )
    skip_reason = models.CharField(
        max_length=30,
        choices=CampaignRecipientSkipReason.choices,
        blank=True,
    )
    tracking_token_hash = models.CharField(max_length=255, unique=True, null=True, blank=True)
    unsubscribe_token_hash = models.CharField(max_length=255, unique=True, null=True, blank=True)
    ses_message_id = models.CharField(max_length=255, blank=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    first_opened_at = models.DateTimeField(null=True, blank=True, db_index=True)
    first_clicked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    open_count = models.PositiveIntegerField(default=0)
    click_count = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    class Meta:
        db_table = "campaign_recipients"
        ordering = ["campaign_id", "contact__normalized_email"]
        constraints = [
            models.UniqueConstraint(fields=["campaign", "contact"], name="unique_campaign_recipient_contact"),
        ]
        indexes = [
            models.Index(fields=["campaign", "status"], name="campaign_recip_status_idx"),
            models.Index(fields=["contact", "sent_at"], name="camp_recip_contact_sent_idx"),
        ]

    def __str__(self):
        return f"{self.campaign_id}: {self.email} ({self.status})"


class EmailTemplate(TimeStampedModel):
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="email_templates")
    key = models.SlugField(max_length=120)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    subject = models.CharField(max_length=255)
    html_body = models.TextField(blank=True)
    text_body = models.TextField(blank=True)
    required_context = models.JSONField(default=list, blank=True)
    example_context = models.JSONField(default=dict, blank=True)
    is_transactional = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "email_templates"
        ordering = ["client__slug", "key"]
        constraints = [
            models.UniqueConstraint(fields=["client", "key"], name="unique_email_template_client_key"),
        ]
        indexes = [
            models.Index(fields=["client", "is_transactional"], name="email_tpl_client_tx_idx"),
            models.Index(fields=["client", "is_transactional", "is_active"], name="email_tpl_tx_active_idx"),
        ]

    def __str__(self):
        return f"{self.key} ({self.client.slug})"


class TransactionalMessageStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"
    BOUNCED = "bounced", "Bounced"
    COMPLAINED = "complained", "Complained"


class TransactionalMessage(TimeStampedModel):
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="transactional_messages")
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="transactional_messages")
    email = models.EmailField(max_length=320)
    from_email_id = models.CharField(max_length=80, blank=True)
    from_email = models.EmailField(max_length=320, blank=True)
    template = models.ForeignKey(EmailTemplate, on_delete=models.PROTECT, related_name="transactional_messages")
    template_key = models.CharField(max_length=120)
    status = models.CharField(
        max_length=20,
        choices=TransactionalMessageStatus.choices,
        default=TransactionalMessageStatus.QUEUED,
    )
    idempotency_key = models.CharField(max_length=255, blank=True)
    subject = models.CharField(max_length=255)
    html_body = models.TextField(blank=True)
    text_body = models.TextField(blank=True)
    context = models.JSONField(default=dict, blank=True)
    ses_message_id = models.CharField(max_length=255, blank=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    first_opened_at = models.DateTimeField(null=True, blank=True)
    first_clicked_at = models.DateTimeField(null=True, blank=True)
    open_count = models.PositiveIntegerField(default=0)
    click_count = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        db_table = "transactional_messages"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="unique_transactional_client_idempotency",
            ),
        ]
        indexes = [
            models.Index(fields=["contact", "created_at"], name="tx_msg_contact_created_idx"),
            models.Index(fields=["client", "status", "created_at"], name="tx_msg_cli_status_created_idx"),
        ]

    def __str__(self):
        return f"{self.email} {self.template_key} ({self.status})"


class EmailEventType(models.TextChoices):
    QUEUED = "queued", "Queued"
    SKIPPED = "skipped", "Skipped"
    SENT = "sent", "Sent"
    DELIVERED = "delivered", "Delivered"
    OPEN = "open", "Open"
    CLICK = "click", "Click"
    SUBSCRIBE = "subscribe", "Subscribe"
    UNSUBSCRIBE = "unsubscribe", "Unsubscribe"
    BOUNCE = "bounce", "Bounce"
    COMPLAINT = "complaint", "Complaint"
    FAILED = "failed", "Failed"


class EmailEvent(models.Model):
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, null=True, blank=True, related_name="events")
    campaign_recipient = models.ForeignKey(
        CampaignRecipient,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="events",
    )
    transactional_message = models.ForeignKey(
        TransactionalMessage,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="events",
    )
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, null=True, blank=True, related_name="email_events")
    client = models.ForeignKey(Client, on_delete=models.PROTECT, null=True, blank=True, related_name="email_events")
    audience = models.ForeignKey(Audience, on_delete=models.PROTECT, null=True, blank=True, related_name="email_events")
    event_type = models.CharField(max_length=20, choices=EmailEventType.choices)
    provider_event_id = models.CharField(max_length=255, blank=True)
    url = models.URLField(max_length=2048, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "email_events"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["contact", "created_at"], name="email_evt_contact_created_idx"),
            models.Index(fields=["campaign", "event_type", "created_at"], name="email_events_campaign_type_idx"),
            models.Index(fields=["campaign_recipient", "event_type"], name="email_evt_recipient_type_idx"),
            models.Index(fields=["client", "created_at"], name="email_evt_client_created_idx"),
            models.Index(fields=["provider_event_id"], name="email_events_provider_evt_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["provider_event_id"],
                condition=~models.Q(provider_event_id=""),
                name="unique_nonempty_provider_event_id",
            ),
        ]

    def __str__(self):
        return f"{self.event_type} at {self.created_at}"


class CmpCallbackStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"


class CmpCallback(TimeStampedModel):
    email_event = models.OneToOneField(
        EmailEvent,
        on_delete=models.CASCADE,
        related_name="cmp_callback",
    )
    contact = models.ForeignKey(
        Contact,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cmp_callbacks",
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cmp_callbacks",
    )
    audience = models.ForeignKey(
        Audience,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cmp_callbacks",
    )
    event_id = models.CharField(max_length=120, unique=True)
    event_type = models.CharField(max_length=80)
    callback_url = models.URLField(max_length=2048)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20,
        choices=CmpCallbackStatus.choices,
        default=CmpCallbackStatus.PENDING,
    )
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=8)
    next_attempt_at = models.DateTimeField(db_index=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    response_status = models.PositiveIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        db_table = "cmp_callbacks"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="cmp_cb_status_next_idx"),
            models.Index(fields=["client", "status", "created_at"], name="cmp_cb_client_status_idx"),
        ]

    def __str__(self):
        return f"{self.event_type} {self.event_id} ({self.status})"


class OperatorAudit(models.Model):
    actor = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="operator_audits"
    )
    action = models.CharField(max_length=120, db_index=True)
    target_type = models.CharField(max_length=80, db_index=True)
    target_id = models.PositiveBigIntegerField(db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "operator_audits"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["target_type", "target_id", "created_at"], name="op_audit_target_created_idx"),
            models.Index(fields=["actor", "created_at"], name="op_audit_actor_created_idx"),
        ]

    def __str__(self):
        actor = self.actor.username if self.actor_id else "unknown"
        return f"{self.action} {self.target_type}:{self.target_id} by {actor}"
