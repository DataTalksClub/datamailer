from django import forms

from mailing.models import Audience, Campaign, CampaignStatus, Client, Tag


class CampaignForm(forms.ModelForm):
    include_tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.select_related("audience").order_by("audience__slug", "slug"),
        required=False,
        widget=forms.SelectMultiple,
    )
    exclude_tags = forms.ModelMultipleChoiceField(
        queryset=Tag.objects.select_related("audience").order_by("audience__slug", "slug"),
        required=False,
        widget=forms.SelectMultiple,
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
            "html_body": forms.Textarea(attrs={"rows": 14}),
            "text_body": forms.Textarea(attrs={"rows": 10}),
            "preview_text": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["html_body"].label = "HTML body"
        self.fields["text_body"].label = "Text body"
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
