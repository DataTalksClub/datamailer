from django import forms
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["html_body"].label = "HTML body"
        self.fields["html_body"].help_text = "Paste the final HTML email body prepared outside Datamailer."
        self.fields["text_body"].label = "Text body"
        self.fields["text_body"].help_text = "Paste the final plain-text fallback. Keep it aligned with the HTML body."
        self.fields["subject"].help_text = "Use the final subject line that recipients will see."
        self.fields["preview_text"].help_text = "Optional inbox preview text shown after the subject by many email clients."
        self.fields["scheduled_at"].help_text = "Optional. Leave blank to keep the draft unscheduled."
        self.fields["audience"].queryset = Audience.objects.select_related("organization").order_by(
            "organization__slug", "slug"
        )
        self.fields["client"].queryset = Client.objects.select_related("organization").filter(is_active=True).order_by(
            "organization__slug", "slug"
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["organization"].queryset = Organization.objects.order_by("slug")

    def clean_slug(self):
        slug = slugify(self.cleaned_data["slug"])
        if not slug:
            raise forms.ValidationError("Enter a valid slug.")
        return slug

    def clean(self):
        cleaned = super().clean()
        organization = cleaned.get("organization")
        slug = cleaned.get("slug")
        if organization and slug:
            duplicate = Audience.objects.filter(organization=organization, slug=slug)
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error("slug", "Audience slug must be unique within this organization.")
        return cleaned


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ["organization", "name", "slug", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["organization"].queryset = Organization.objects.order_by("slug")

    def clean_slug(self):
        slug = slugify(self.cleaned_data["slug"])
        if not slug:
            raise forms.ValidationError("Enter a valid slug.")
        return slug

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
        return cleaned


class ClientApiKeyForm(forms.ModelForm):
    class Meta:
        model = ClientApiKey
        fields = ["name", "notes"]

    def __init__(self, *args, client=None, **kwargs):
        self.client = client
        super().__init__(*args, **kwargs)
        self.fields["notes"].required = False

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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["audience"].queryset = Audience.objects.select_related("organization").order_by(
            "organization__slug", "slug"
        )
        self.fields["client"].queryset = Client.objects.select_related("organization").order_by(
            "organization__slug", "slug"
        )

    def clean(self):
        cleaned = super().clean()
        audience = cleaned.get("audience")
        client = cleaned.get("client")
        if audience and client and audience.organization_id != client.organization_id:
            self.add_error("client", "Client must belong to the selected audience organization.")
        return cleaned


class ContactTagAddForm(forms.Form):
    audience = forms.ModelChoiceField(queryset=Audience.objects.none())
    tag = forms.ModelChoiceField(queryset=Tag.objects.none(), required=False)
    new_tag_name = forms.CharField(required=False, max_length=120)
    new_tag_slug = forms.SlugField(required=False, max_length=120)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["audience"].queryset = Audience.objects.order_by("slug")
        self.fields["tag"].queryset = Tag.objects.select_related("audience").order_by("audience__slug", "slug")

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

    def __init__(self, *args, contact=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["membership"].queryset = ContactTag.objects.filter(contact=contact).select_related(
            "tag", "tag__audience"
        )
