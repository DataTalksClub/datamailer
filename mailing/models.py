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
