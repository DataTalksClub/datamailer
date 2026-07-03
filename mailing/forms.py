from email.utils import parseaddr

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email, validate_slug
from django.utils.text import slugify

from mailing.models import (
    Audience,
    Campaign,
    CampaignStatus,
    Client,
    ClientApiKey,
    ContactTag,
    EmailValidationStatus,
    Organization,
    SubscriptionStatus,
    Tag,
)


class CampaignForm(forms.ModelForm):
    include_tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.select_related("audience").order_by("audience__slug", "slug"),
        required=False,
        widget=forms.SelectMultiple,
        help_text="Send only to contacts that have every selected include tag. Tags must belong to the selected audience.",
    )
    exclude_tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.select_related("audience").order_by("audience__slug", "slug"),
        required=False,
        widget=forms.SelectMultiple,
        help_text="Skip contacts with any selected exclude tag. A tag cannot be both included and excluded.",
    )
    scheduled_at = forms.DateTimeField(
        required=False,
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )

    class Meta:
        model = Campaign
        fields = [
            "audience",
            "client",
            "subject",
            "preview_text",
            "html_body",
            "text_body",
            "scheduled_at",
        ]
        widgets = {
            "html_body": forms.Textarea(attrs={"rows": 18}),
            "text_body": forms.Textarea(attrs={"rows": 12}),
            "preview_text": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, active_client=None, **kwargs):
        self.active_client = active_client
        super().__init__(*args, **kwargs)
        self.fields["html_body"].label = "HTML body"
        self.fields["html_body"].help_text = "Paste the final HTML email body prepared outside Datamailer."
        self.fields["text_body"].label = "Text body"
        self.fields["text_body"].help_text = "Paste the final plain-text fallback. Keep it aligned with the HTML body."
        self.fields["subject"].help_text = "Use the final subject line that recipients will see."
        self.fields[
            "preview_text"
        ].help_text = "Optional inbox preview text shown after the subject by many email clients."
        self.fields["scheduled_at"].help_text = "Optional. Leave blank to keep the draft unscheduled."
        self.fields["audience"].queryset = Audience.objects.select_related("organization").order_by(
            "organization__slug", "slug"
        )
        self.fields["client"].queryset = (
            Client.objects.select_related("organization").filter(is_active=True).order_by("organization__slug", "slug")
        )
        if active_client is not None:
            self.fields["client"].queryset = Client.objects.filter(pk=active_client.pk)
            self.fields["client"].initial = active_client
            self.fields["client"].widget = forms.HiddenInput()
            self.fields["audience"].queryset = (
                Audience.objects.select_related("organization")
                .filter(organization=active_client.organization)
                .order_by("slug")
            )
            self.fields["include_tags"].queryset = (
                Tag.objects.select_related("audience")
                .filter(audience__organization=active_client.organization)
                .order_by("audience__slug", "slug")
            )
            self.fields["exclude_tags"].queryset = (
                Tag.objects.select_related("audience")
                .filter(audience__organization=active_client.organization)
                .order_by("audience__slug", "slug")
            )
        if self.instance and self.instance.pk:
            self.fields["include_tags"].initial = Tag.objects.filter(
                audience=self.instance.audience,
                slug__in=self.instance.include_tags,
            )
            self.fields["exclude_tags"].initial = Tag.objects.filter(
                audience=self.instance.audience,
                slug__in=self.instance.exclude_tags,
            )
            if self.instance.status != CampaignStatus.DRAFT:
                for field in self.fields.values():
                    field.disabled = True

    def clean(self):
        cleaned_data = super().clean()
        audience = cleaned_data.get("audience")
        client = cleaned_data.get("client")
        html_body = (cleaned_data.get("html_body") or "").strip()
        text_body = (cleaned_data.get("text_body") or "").strip()
        include_tags = list(cleaned_data.get("include_tags") or [])
        exclude_tags = list(cleaned_data.get("exclude_tags") or [])

        if self.instance and self.instance.pk and self.instance.status != CampaignStatus.DRAFT:
            raise forms.ValidationError("Queued or sent campaigns cannot be edited from this form.")

        if self.active_client is not None:
            cleaned_data["client"] = self.active_client
            client = self.active_client

        if audience and client and audience.organization_id != client.organization_id:
            self.add_error("client", "Client must belong to the selected audience organization.")

        if not html_body:
            self.add_error("html_body", "Paste the final HTML body before saving.")
        if not text_body:
            self.add_error("text_body", "Paste the final text body before saving.")

        for field_name, selected_tags in (("include_tags", include_tags), ("exclude_tags", exclude_tags)):
            if audience and any(tag.audience_id != audience.id for tag in selected_tags):
                self.add_error(field_name, "Tags must belong to the selected audience.")

        include_ids = {tag.id for tag in include_tags}
        exclude_ids = {tag.id for tag in exclude_tags}
        if include_ids & exclude_ids:
            self.add_error("exclude_tags", "A tag cannot be both included and excluded.")

        return cleaned_data

    def save(self, commit=True):
        campaign = super().save(commit=False)
        campaign.include_tags = [tag.slug for tag in self.cleaned_data["include_tags"]]
        campaign.exclude_tags = [tag.slug for tag in self.cleaned_data["exclude_tags"]]
        if commit:
            campaign.save()
        return campaign


class AudienceForm(forms.ModelForm):
    class Meta:
        model = Audience
        fields = ["organization", "name", "slug"]

    def __init__(self, *args, active_client=None, **kwargs):
        self.active_client = active_client
        super().__init__(*args, **kwargs)
        self.fields["organization"].queryset = Organization.objects.order_by("slug")
        self.fields["organization"].help_text = "The selected organization scopes this audience and its slug."
        self.fields["name"].label = "Audience name"
        self.fields["name"].help_text = "Operator-facing name shown in lists and campaign setup."
        self.fields["slug"].label = "Audience slug"
        self.fields["slug"].help_text = "Lowercase identifier; must be unique within the selected organization."
        if active_client is not None:
            self.fields["organization"].queryset = Organization.objects.filter(pk=active_client.organization_id)
            self.fields["organization"].initial = active_client.organization
            self.fields["organization"].widget = forms.HiddenInput()

    def clean_slug(self):
        slug = slugify(self.cleaned_data["slug"])
        if not slug:
            raise forms.ValidationError("Enter a valid slug.")
        return slug

    def clean(self):
        cleaned = super().clean()
        organization = cleaned.get("organization")
        slug = cleaned.get("slug")
        if self.active_client is not None:
            cleaned["organization"] = self.active_client.organization
            organization = self.active_client.organization
        if organization and slug:
            duplicate = Audience.objects.filter(organization=organization, slug=slug)
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error("slug", "Audience slug must be unique within this organization.")
        return cleaned


class ClientForm(forms.ModelForm):
    sender_emails = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    class Meta:
        model = Client
        fields = [
            "organization",
            "name",
            "slug",
            "default_sender_id",
            "sender_emails",
            "cmp_webhook_url",
            "cmp_webhook_token",
            "mailchimp_api_key",
            "mailchimp_list_id",
            "mailchimp_enabled",
            "is_active",
        ]
        widgets = {
            "cmp_webhook_token": forms.PasswordInput(render_value=True),
            "mailchimp_api_key": forms.PasswordInput(render_value=False),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["organization"].queryset = Organization.objects.order_by("slug")
        self.fields["organization"].help_text = "The organization that owns this integration and scopes its slug."
        self.fields["name"].label = "Client name"
        self.fields[
            "name"
        ].help_text = "Operator-facing name for the product, app, or external system using Datamailer."
        self.fields["slug"].label = "Client slug"
        self.fields["slug"].help_text = (
            "Stable identifier used in URLs, support conversations, logs, audit context, and API examples. "
            "It is not a secret or API key."
        )
        self.fields["default_sender_id"].label = "Default sender ID"
        self.fields["default_sender_id"].help_text = "Used when the API payload does not specify from_email."
        self.fields["sender_emails"].label = "Configured senders"
        self.fields["sender_emails"].help_text = (
            "One sender per line as sender-id=email@example.com or "
            "sender-id=Display Name <email@example.com>. API payload "
            "from_email must use a configured sender ID."
        )
        self.fields["cmp_webhook_url"].label = "CMP webhook URL"
        self.fields["cmp_webhook_url"].required = False
        self.fields["cmp_webhook_url"].help_text = (
            "Optional client-specific callback endpoint for delivery, suppression, "
            "unsubscribe, and transactional failure events."
        )
        self.fields["cmp_webhook_token"].label = "CMP webhook token"
        self.fields["cmp_webhook_token"].required = False
        self.fields["cmp_webhook_token"].help_text = (
            "Bearer token Datamailer sends to the CMP webhook. Leave empty to use global settings, if configured."
        )
        self.fields["mailchimp_api_key"].label = "Mailchimp API key"
        self.fields["mailchimp_api_key"].required = False
        self.fields["mailchimp_api_key"].help_text = (
            "Mailchimp API key including the datacenter suffix (e.g. abc123...-us21). "
            "Stored write-only. Leave blank when editing to keep the current key."
        )
        self.fields["mailchimp_list_id"].label = "Mailchimp audience ID"
        self.fields["mailchimp_list_id"].required = False
        self.fields["mailchimp_list_id"].help_text = (
            "The Mailchimp audience (list) ID that tagged contacts are synced into."
        )
        self.fields["mailchimp_enabled"].label = "Enable Mailchimp sync"
        self.fields["mailchimp_enabled"].help_text = (
            "When on, contacts added to mapped recipient-list nodes are pushed to Mailchimp with the mapped tag."
        )
        if self.instance and self.instance.pk:
            self.fields["sender_emails"].initial = "\n".join(
                f"{sender.get('id')}={sender.get('email')}"
                for sender in self.instance.sender_emails or []
                if isinstance(sender, dict) and sender.get("id") and sender.get("email")
            )
        self.fields["is_active"].label = "Client is active"
        self.fields[
            "is_active"
        ].help_text = "Inactive clients cannot use their API keys for authenticated API activity or sending."

    def clean_slug(self):
        slug = slugify(self.cleaned_data["slug"])
        if not slug:
            raise forms.ValidationError("Enter a valid slug.")
        return slug

    def clean_default_sender_id(self):
        sender_id = (self.cleaned_data.get("default_sender_id") or "").strip()
        if sender_id:
            try:
                validate_slug(sender_id)
            except ValidationError as exc:
                raise forms.ValidationError(
                    "Enter a valid sender ID using letters, numbers, hyphens, or underscores."
                ) from exc
        return sender_id

    def clean_sender_emails(self):
        raw_lines = (self.cleaned_data.get("sender_emails") or "").splitlines()
        senders = []
        seen_ids = set()
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            if "=" in line:
                sender_id, email = line.split("=", 1)
            elif "," in line:
                sender_id, email = line.split(",", 1)
            else:
                parts = line.split(None, 1)
                if len(parts) != 2:
                    raise forms.ValidationError("Use sender-id=email@example.com, one sender per line.")
                sender_id, email = parts
            sender_id = sender_id.strip()
            email = email.strip()
            try:
                validate_slug(sender_id)
            except ValidationError as exc:
                raise forms.ValidationError(f"Enter a valid sender ID: {sender_id}") from exc
            _, parsed_email = parseaddr(email)
            email_to_validate = parsed_email or email
            if parsed_email and parsed_email not in email:
                raise forms.ValidationError(f"Enter a valid sender email address: {email}")
            try:
                validate_email(email_to_validate)
            except ValidationError as exc:
                raise forms.ValidationError(f"Enter a valid sender email address: {email}") from exc
            if sender_id in seen_ids:
                raise forms.ValidationError(f"Sender ID is duplicated: {sender_id}")
            senders.append({"id": sender_id, "email": email})
            seen_ids.add(sender_id)
        return senders

    def clean(self):
        cleaned = super().clean()
        organization = cleaned.get("organization")
        slug = cleaned.get("slug")
        if organization and slug:
            duplicate = Client.objects.filter(organization=organization, slug=slug)
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error("slug", "Client slug must be unique within this organization.")
        sender_emails = cleaned.get("sender_emails") or []
        default_sender_id = cleaned.get("default_sender_id")
        sender_ids = {sender["id"] for sender in sender_emails}
        if sender_emails and not default_sender_id:
            cleaned["default_sender_id"] = sender_emails[0]["id"]
        elif default_sender_id and default_sender_id not in sender_ids:
            self.add_error("default_sender_id", "Default sender ID must be listed in configured senders.")
        return cleaned


class ClientApiKeyForm(forms.ModelForm):
    class Meta:
        model = ClientApiKey
        fields = ["name", "notes"]

    def __init__(self, *args, client=None, **kwargs):
        self.client = client
        super().__init__(*args, **kwargs)
        self.fields["name"].label = "Key name"
        self.fields[
            "name"
        ].help_text = "Use a human-readable integration name, such as website signup or course platform."
        self.fields["notes"].label = "Purpose and notes"
        self.fields["notes"].required = False
        self.fields["notes"].help_text = "Describe what uses this key. Do not store raw secrets here."

    def clean_name(self):
        return self.cleaned_data["name"].strip()

    def clean(self):
        cleaned = super().clean()
        name = cleaned.get("name")
        if self.client and name:
            duplicate = ClientApiKey.objects.filter(client=self.client, name=name, revoked_at__isnull=True)
            if duplicate.exists():
                self.add_error("name", "Active API key names must be unique for this client.")
        return cleaned


class TagForm(forms.ModelForm):
    class Meta:
        model = Tag
        fields = ["name", "slug"]

    def __init__(self, *args, audience=None, **kwargs):
        self.audience = audience or getattr(kwargs.get("instance"), "audience", None)
        super().__init__(*args, **kwargs)
        self.fields["name"].label = "Tag name"
        self.fields["name"].help_text = "Operator-facing segment name inside this audience."
        self.fields["slug"].label = "Tag slug"
        self.fields["slug"].help_text = "Lowercase identifier; must be unique within this audience."

    def clean_slug(self):
        slug = slugify(self.cleaned_data["slug"] or self.cleaned_data.get("name", ""))
        if not slug:
            raise forms.ValidationError("Enter a valid slug.")
        return slug

    def clean(self):
        cleaned = super().clean()
        slug = cleaned.get("slug")
        if self.audience and slug:
            duplicate = Tag.objects.filter(audience=self.audience, slug=slug)
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error("slug", "Tag slug must be unique within this audience.")
        return cleaned


class ContactStateForm(forms.Form):
    verified_state = forms.ChoiceField(
        choices=(("unchanged", "Leave unchanged"), ("verified", "Verified"), ("unverified", "Unverified")),
    )
    email_validation_status = forms.ChoiceField(choices=EmailValidationStatus.choices)
    email_validation_reason = forms.CharField(required=False, max_length=255)
    global_unsubscribed = forms.BooleanField(required=False)
    hard_bounced = forms.BooleanField(required=False)
    complained = forms.BooleanField(required=False)


class ContactSubscriptionForm(forms.Form):
    audience = forms.ModelChoiceField(queryset=Audience.objects.none())
    client = forms.ModelChoiceField(queryset=Client.objects.none(), required=False)
    status = forms.ChoiceField(choices=SubscriptionStatus.choices)
    verified = forms.BooleanField(required=False)
    unsubscribe_reason = forms.CharField(required=False, max_length=255)

    def __init__(self, *args, active_client=None, **kwargs):
        self.active_client = active_client
        super().__init__(*args, **kwargs)
        self.fields["audience"].queryset = Audience.objects.select_related("organization").order_by(
            "organization__slug", "slug"
        )
        self.fields["client"].queryset = Client.objects.select_related("organization").order_by(
            "organization__slug", "slug"
        )
        if active_client is not None:
            self.fields["audience"].queryset = (
                Audience.objects.select_related("organization")
                .filter(organization=active_client.organization)
                .order_by("slug")
            )
            self.fields["client"].queryset = Client.objects.filter(pk=active_client.pk)
            self.fields["client"].initial = active_client
            self.fields["client"].widget = forms.HiddenInput()

    def clean(self):
        cleaned = super().clean()
        audience = cleaned.get("audience")
        client = cleaned.get("client")
        if self.active_client is not None:
            cleaned["client"] = self.active_client
            client = self.active_client
        if audience and client and audience.organization_id != client.organization_id:
            self.add_error("client", "Client must belong to the selected audience organization.")
        return cleaned


class ContactTagAddForm(forms.Form):
    audience = forms.ModelChoiceField(queryset=Audience.objects.none())
    tag = forms.ModelChoiceField(queryset=Tag.objects.none(), required=False)
    new_tag_name = forms.CharField(required=False, max_length=120)
    new_tag_slug = forms.SlugField(required=False, max_length=120)

    def __init__(self, *args, active_client=None, **kwargs):
        self.active_client = active_client
        super().__init__(*args, **kwargs)
        self.fields["audience"].queryset = Audience.objects.order_by("slug")
        self.fields["tag"].queryset = Tag.objects.select_related("audience").order_by("audience__slug", "slug")
        if active_client is not None:
            self.fields["audience"].queryset = Audience.objects.filter(
                organization=active_client.organization
            ).order_by("slug")
            self.fields["tag"].queryset = (
                Tag.objects.select_related("audience")
                .filter(audience__organization=active_client.organization)
                .order_by("audience__slug", "slug")
            )

    def clean(self):
        cleaned = super().clean()
        audience = cleaned.get("audience")
        tag = cleaned.get("tag")
        new_name = cleaned.get("new_tag_name")
        if not tag and not new_name:
            raise forms.ValidationError("Choose an existing tag or enter a new tag name.")
        if audience and tag and tag.audience_id != audience.id:
            self.add_error("tag", "Tag must belong to the selected audience.")
        return cleaned


class ContactTagRemoveForm(forms.Form):
    membership = forms.ModelChoiceField(queryset=ContactTag.objects.none())

    def __init__(self, *args, contact=None, active_client=None, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = ContactTag.objects.filter(contact=contact).select_related("tag", "tag__audience")
        if active_client is not None:
            queryset = queryset.filter(tag__audience__organization=active_client.organization)
        self.fields["membership"].queryset = queryset
