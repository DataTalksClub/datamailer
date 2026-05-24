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
    api_key_hash = models.CharField(max_length=255, blank=True)
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


class Contact(TimeStampedModel):
    email = models.EmailField(max_length=320)
    normalized_email = models.EmailField(max_length=320, unique=True)
    verified_at = models.DateTimeField(null=True, blank=True, db_index=True)
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
            models.Index(fields=["audience", "client", "status"], name="subs_audience_client_status_idx"),
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
            models.Index(fields=["contact", "sent_at"], name="campaign_recip_contact_sent_idx"),
        ]

    def __str__(self):
        return f"{self.campaign_id}: {self.email} ({self.status})"
