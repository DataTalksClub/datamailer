from django.contrib import admin

from mailing.models import (
    Audience,
    Campaign,
    CampaignRecipient,
    Client,
    Contact,
    ContactTag,
    Organization,
    Subscription,
    Tag,
)


class CreatedAtReadOnlyMixin:
    readonly_fields = ("created_at",)


@admin.register(Organization)
class OrganizationAdmin(CreatedAtReadOnlyMixin, admin.ModelAdmin):
    list_display = ("name", "slug", "created_at")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Audience)
class AudienceAdmin(CreatedAtReadOnlyMixin, admin.ModelAdmin):
    list_display = ("name", "slug", "organization", "created_at")
    list_filter = ("organization",)
    search_fields = ("name", "slug", "organization__name", "organization__slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Client)
class ClientAdmin(CreatedAtReadOnlyMixin, admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at")
    list_display = ("name", "slug", "organization", "is_active", "created_at")
    list_filter = ("organization", "is_active")
    search_fields = ("name", "slug", "organization__name", "organization__slug")
    prepopulated_fields = {"slug": ("name",)}


class SubscriptionInline(admin.TabularInline):
    model = Subscription
    extra = 0
    fields = ("audience", "client", "status", "verified_at", "unsubscribed_at", "unsubscribe_reason")
    autocomplete_fields = ("audience", "client")


class ContactTagInline(admin.TabularInline):
    model = ContactTag
    extra = 0
    fields = ("tag", "created_at")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("tag",)


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at")
    list_display = (
        "email",
        "normalized_email",
        "verified_at",
        "global_unsubscribed_at",
        "hard_bounced_at",
        "complained_at",
        "updated_at",
    )
    list_filter = ("verified_at", "global_unsubscribed_at", "hard_bounced_at", "complained_at")
    search_fields = ("email", "normalized_email", "subscriptions__audience__slug", "subscriptions__client__slug")
    inlines = (SubscriptionInline, ContactTagInline)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at")
    list_display = ("contact", "audience", "client", "status", "verified_at", "unsubscribed_at", "updated_at")
    list_filter = ("status", "audience", "client")
    search_fields = (
        "contact__email",
        "contact__normalized_email",
        "audience__name",
        "audience__slug",
        "client__name",
        "client__slug",
    )
    autocomplete_fields = ("contact", "audience", "client")


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "audience")
    list_filter = ("audience",)
    search_fields = ("name", "slug", "audience__name", "audience__slug")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("audience",)


@admin.register(ContactTag)
class ContactTagAdmin(CreatedAtReadOnlyMixin, admin.ModelAdmin):
    list_display = ("contact", "tag", "created_at")
    list_filter = ("tag__audience", "tag")
    search_fields = ("contact__email", "contact__normalized_email", "tag__name", "tag__slug")
    autocomplete_fields = ("contact", "tag")


class CampaignRecipientInline(admin.TabularInline):
    model = CampaignRecipient
    extra = 0
    fields = ("contact", "email", "status", "skip_reason", "sent_at", "last_error")
    readonly_fields = ("email", "status", "skip_reason", "sent_at", "last_error")
    autocomplete_fields = ("contact",)
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    readonly_fields = (
        "created_at",
        "updated_at",
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
    )
    list_display = (
        "subject",
        "client",
        "audience",
        "status",
        "scheduled_at",
        "recipient_count",
        "skipped_count",
        "sent_count",
        "created_at",
    )
    list_filter = ("status", "client", "audience")
    search_fields = ("subject", "client__name", "client__slug", "audience__name", "audience__slug")
    autocomplete_fields = ("client", "audience")
    inlines = (CampaignRecipientInline,)


@admin.register(CampaignRecipient)
class CampaignRecipientAdmin(admin.ModelAdmin):
    readonly_fields = ("created_at", "updated_at")
    list_display = ("campaign", "email", "contact", "status", "skip_reason", "sent_at", "last_error")
    list_filter = ("status", "skip_reason", "campaign__client", "campaign__audience")
    search_fields = (
        "email",
        "contact__email",
        "contact__normalized_email",
        "campaign__subject",
        "campaign__client__name",
        "campaign__client__slug",
        "campaign__audience__name",
        "campaign__audience__slug",
        "ses_message_id",
    )
    autocomplete_fields = ("campaign", "contact")
