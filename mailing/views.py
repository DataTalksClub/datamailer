import json

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from mailing.context_processors import ACTIVE_CLIENT_SESSION_KEY
from mailing.forms import (
    AudienceForm,
    CampaignForm,
    ClientApiKeyForm,
    ClientForm,
    ContactStateForm,
    ContactSubscriptionForm,
    ContactTagAddForm,
    ContactTagRemoveForm,
    TagForm,
)
from mailing.models import (
    Audience,
    Campaign,
    CampaignStatus,
    Client,
    ClientApiKey,
    CmpCallback,
    EmailEvent,
    EmailEventType,
    MailchimpSync,
    MailchimpTagMapping,
    Tag,
    TransactionalMessage,
)
from mailing.services.api import (
    ApiValidationError,
    add_contact_tag_for_client,
    cancel_campaign_for_client,
    erase_contact_for_client,
    get_campaign_for_client,
    get_client_sender_policy_for_client,
    get_contact_history_for_client,
    get_contact_preferences_for_client,
    get_contact_status_for_client,
    get_transactional_message_status_for_client,
    get_transactional_template_for_client,
    preview_campaign_for_client,
    queue_campaign_for_client,
    remove_contact_tag_for_client,
    replace_contact_tags_for_client,
    subscribe_for_client,
    test_send_campaign_for_client,
    unsubscribe_for_client,
    update_client_sender_policy_for_client,
    update_contact_preferences_for_client,
    update_contact_suppression_for_client,
    update_contact_validation_for_client,
    update_contact_verification_for_client,
    upsert_campaign_for_client,
    upsert_contact_for_client,
    upsert_transactional_template_for_client,
    validate_contact_scope,
)
from mailing.services.api_docs import (
    DEMO_API_KEYS,
    build_openapi_spec,
    docs_base_url,
    endpoint_groups,
    workflow_examples,
)
from mailing.services.auth import authenticate_bearer_token
from mailing.services.campaigns import estimate_campaign_recipients, queue_campaign
from mailing.services.contact_import_export import (
    bulk_import_contacts_for_client,
    csv_import_contacts_for_client,
    export_contacts_csv_for_client,
    export_contacts_for_client,
)
from mailing.services.mailchimp import (
    mailchimp_status_payload,
    reconcile_tag_mappings_for_client,
    update_mailchimp_config_for_client,
)
from mailing.services.operator_management import (
    add_contact_tag,
    client_api_keys_for_detail,
    create_api_key,
    create_or_update_audience,
    create_or_update_client,
    create_or_update_tag,
    latest_audits_for,
    remove_contact_tag,
    revoke_api_key,
    update_contact_state,
    update_subscription,
)
from mailing.services.operator_ui import (
    RECIPIENT_FILTER_LABELS,
    Badge,
    active_contact_filters,
    audience_breakdowns,
    audience_campaign_history,
    audience_detail_queryset,
    audience_list_rows,
    audience_queryset,
    audience_recent_events,
    audience_summary,
    campaign_list_rows,
    campaign_queryset,
    campaign_recent_events,
    campaign_recipient_queryset,
    campaign_send_progress,
    campaign_stats,
    campaign_status_badge,
    choices_from_text_choices,
    contact_campaign_history,
    contact_detail_context,
    contact_detail_queryset,
    contact_event_timeline,
    contact_explorer_options,
    contact_explorer_queryset,
    contact_result_rows,
    contact_transactional_history,
    dashboard_context,
    delivery_tone,
    event_context,
    metadata_summary,
    parse_contact_explorer_filters,
)
from mailing.services.recipient_lists import (
    bulk_upsert_recipient_list_members_for_client,
    create_recipient_list_import_job_for_client,
    get_recipient_list_for_client,
    get_recipient_list_import_job_for_client,
    get_recipient_list_members_for_client,
    reconcile_recipient_list_for_client,
    remove_recipient_list_member_for_client,
    upsert_recipient_list_for_client,
    upsert_recipient_list_member_for_client,
)
from mailing.services.ses_webhooks import SesWebhookError, SnsSignatureError, ingest_sns_webhook
from mailing.services.tokens import get_recipient_by_unsubscribe_token
from mailing.services.tracking import TRANSPARENT_GIF, apply_unsubscribe, record_click, record_open
from mailing.services.transactional import (
    TransactionalSendRejected,
    send_transactional_email_for_client,
    send_transactional_email_to_recipient_list_for_client,
    send_transactional_email_to_transient_recipient_list_for_client,
)
from mailing.services.transactional_catalog import (
    catalog_context,
    filter_transactional_templates,
    recent_message_rows,
    template_catalog_rows,
    transactional_queue_queryset,
    transactional_template_queryset,
)
from mailing.services.worker_status import worker_status_payload


def health(request):
    return JsonResponse({"status": "ok"})


def active_operator_client(request):
    client_id = request.session.get(ACTIVE_CLIENT_SESSION_KEY)
    if not client_id:
        only_client = Client.objects.select_related("organization").first()
        if only_client is not None and not Client.objects.exclude(pk=only_client.pk).exists():
            request.session[ACTIVE_CLIENT_SESSION_KEY] = only_client.id
            return only_client
        return None
    client = Client.objects.select_related("organization").filter(pk=client_id).first()
    if client is None:
        request.session.pop(ACTIVE_CLIENT_SESSION_KEY, None)
    return client


def scoped_redirect_url(request):
    fallback = reverse("mailing:dashboard")
    next_url = request.POST.get("next") or request.GET.get("next") or fallback
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return next_url
    return fallback


def require_active_client(request):
    client = active_operator_client(request)
    if client is None:
        messages.info(request, "Select an active client before using this section.")
    return client


@staff_member_required
def dashboard(request):
    active_client = active_operator_client(request)
    return render(
        request,
        "mailing/dashboard.html",
        {
            "dashboard": dashboard_context(active_client),
            "active_client": active_client,
            "clients": Client.objects.select_related("organization").order_by("organization__slug", "slug"),
        },
    )


def paginate(request, queryset, *, per_page):
    paginator = Paginator(queryset, per_page)
    return paginator.get_page(request.GET.get("page"))


def pagination_querystring(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


@staff_member_required
def api_docs(request):
    base_url = docs_base_url()
    return render(
        request,
        "mailing/operator/api_docs.html",
        {
            "endpoint_groups": endpoint_groups(),
            "workflow_examples": workflow_examples(base_url),
            "demo_api_keys": DEMO_API_KEYS,
            "docs_base_url": base_url,
            "openapi_json_url": "mailing:api_docs_json",
        },
    )


@staff_member_required
def api_docs_json(request):
    return JsonResponse(build_openapi_spec(request), json_dumps_params={"indent": 2})


@staff_member_required
@require_GET
def api_worker_status(request):
    return JsonResponse(worker_status_payload())


@staff_member_required
def template_catalog(request):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    templates = paginate(
        request,
        filter_transactional_templates(transactional_template_queryset(), str(active_client.id)),
        per_page=25,
    )
    return render(
        request,
        "mailing/operator/template_catalog.html",
        {
            "templates": templates,
            "template_rows": template_catalog_rows(templates.object_list),
            "active_client": active_client,
            "pagination_querystring": pagination_querystring(request),
        },
    )


@staff_member_required
def template_detail(request, template_id):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    template = get_object_or_404(transactional_template_queryset(), pk=template_id, client=active_client)
    recent_messages = template.transactional_messages.select_related("contact").order_by("-created_at", "-id")[:10]
    return render(
        request,
        "mailing/operator/template_detail.html",
        catalog_context(template) | {"recent_message_rows": recent_message_rows(recent_messages)},
    )


@staff_member_required
def transactional_queue(request):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    queue = paginate(request, transactional_queue_queryset(active_client), per_page=25)
    return render(
        request,
        "mailing/operator/transactional_queue.html",
        {
            "active_client": active_client,
            "queue": queue,
            "message_rows": recent_message_rows(queue.object_list),
            "queued_total": queue.paginator.count,
            "pagination_querystring": pagination_querystring(request),
        },
    )


@staff_member_required
def transactional_message_detail(request, message_id):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    message = get_object_or_404(
        TransactionalMessage.objects.select_related("client", "contact", "template"),
        pk=message_id,
        client=active_client,
    )
    events = (
        EmailEvent.objects.filter(transactional_message=message)
        .select_related("contact", "client", "audience", "campaign")
        .order_by("-created_at", "-id")
    )
    event_rows = [
        {"event": event, "context": event_context(event), "metadata_summary": metadata_summary(event.metadata)}
        for event in events
    ]
    callback_rows = CmpCallback.objects.filter(
        email_event__transactional_message=message,
    ).order_by("-created_at", "-id")
    return render(
        request,
        "mailing/operator/transactional_message_detail.html",
        {
            "message": message,
            "badge": Badge(message.get_status_display(), delivery_tone(message.status)),
            "event_rows": event_rows,
            "callback_rows": callback_rows,
            "metadata_summary": metadata_summary(message.metadata),
        },
    )


@staff_member_required
def campaign_list(request):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    campaigns = paginate(request, campaign_queryset(active_client), per_page=25)
    return render(
        request,
        "mailing/operator/campaign_list.html",
        {
            "active_client": active_client,
            "campaigns": campaigns,
            "campaign_rows": campaign_list_rows(campaigns.object_list),
            "pagination_querystring": pagination_querystring(request),
        },
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def campaign_create(request):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    form = CampaignForm(request.POST or None, active_client=active_client)
    if request.method == "POST" and form.is_valid():
        campaign = form.save()
        messages.success(request, "Campaign draft created.")
        return redirect("mailing:campaign_detail", campaign_id=campaign.id)

    return render(
        request,
        "mailing/operator/campaign_form.html",
        {"form": form, "mode": "create", "campaign": None, "active_client": active_client},
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def campaign_edit(request, campaign_id):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    campaign = get_object_or_404(
        Campaign.objects.select_related("client", "audience"), pk=campaign_id, client=active_client
    )
    form = CampaignForm(request.POST or None, instance=campaign, active_client=active_client)
    if request.method == "POST" and form.is_valid():
        campaign = form.save()
        messages.success(request, "Campaign draft updated.")
        return redirect("mailing:campaign_detail", campaign_id=campaign.id)

    return render(
        request,
        "mailing/operator/campaign_form.html",
        {"form": form, "mode": "edit", "campaign": campaign, "active_client": active_client},
    )


@staff_member_required
def campaign_detail(request, campaign_id):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    campaign = get_object_or_404(
        Campaign.objects.select_related("client", "audience", "audience__organization"),
        pk=campaign_id,
        client=active_client,
    )
    active_filter = request.GET.get("filter", "")
    recipients = paginate(request, campaign_recipient_queryset(campaign, active_filter), per_page=50)
    estimate = estimate_campaign_recipients(campaign) if campaign.status == CampaignStatus.DRAFT else None
    events = campaign_recent_events(campaign)[:10]
    event_rows = [{"event": event, "metadata_summary": metadata_summary(event.metadata)} for event in events]
    return render(
        request,
        "mailing/operator/campaign_detail.html",
        {
            "campaign": campaign,
            "active_client": active_client,
            "campaign_badge": campaign_status_badge(campaign.status),
            "can_edit": campaign.status == CampaignStatus.DRAFT,
            "can_queue": campaign.status == CampaignStatus.DRAFT,
            "estimate": estimate,
            "stats": campaign_stats(campaign),
            "send_progress": campaign_send_progress(campaign),
            "recipients": recipients,
            "event_rows": event_rows,
            "recipient_filter_labels": RECIPIENT_FILTER_LABELS,
            "active_filter": active_filter if active_filter in RECIPIENT_FILTER_LABELS else "",
            "pagination_querystring": pagination_querystring(request),
        },
    )


@staff_member_required
@require_POST
def campaign_queue(request, campaign_id):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    campaign = get_object_or_404(Campaign, pk=campaign_id, client=active_client)
    result = queue_campaign(campaign)
    if result.queued:
        messages.success(
            request,
            f"Campaign queued with {result.recipient_count} recipients, {result.skipped_count} skipped, "
            f"and {result.batch_count} send batches.",
        )
    else:
        messages.info(request, "Campaign was already queued or locked for sending.")
    return redirect("mailing:campaign_detail", campaign_id=campaign.id)


@staff_member_required
def contact_search(request):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    filters = parse_contact_explorer_filters(request.GET, forced_client_id=active_client.id)
    contacts = paginate(request, contact_explorer_queryset(filters), per_page=25) if filters.has_filters else None
    rows = contact_result_rows(contacts.object_list, client=active_client) if contacts is not None else []
    return render(
        request,
        "mailing/operator/contact_search.html",
        {
            "active_client": active_client,
            "filters": filters,
            "options": contact_explorer_options(active_client),
            "contacts": contacts,
            "contact_rows": rows,
            "active_filters": active_contact_filters(filters),
            "pagination_querystring": pagination_querystring(request),
        },
    )


def get_contact_by_email_or_404(contact_email):
    return get_object_or_404(contact_detail_queryset(), normalized_email=contact_email.casefold())


@staff_member_required
def contact_detail(request, contact_email):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    contact = get_object_or_404(contact_detail_queryset(), normalized_email=contact_email.casefold())
    detail_context = contact_detail_context(contact, active_client)
    events = paginate(request, contact_event_timeline(contact, active_client), per_page=50)
    event_rows = [
        {"event": event, "context": event_context(event), "metadata_summary": metadata_summary(event.metadata)}
        for event in events.object_list
    ]
    campaign_history = paginate(request, contact_campaign_history(contact, active_client), per_page=25)
    transactional_history = paginate(request, contact_transactional_history(contact, active_client), per_page=25)
    transactional_rows = [
        {"message": message, "metadata_summary": metadata_summary(message.metadata)}
        for message in transactional_history.object_list
    ]
    return render(
        request,
        "mailing/operator/contact_detail.html",
        {
            "contact": contact,
            "active_client": active_client,
            "eligibility": detail_context.eligibility,
            "subscriptions": detail_context.subscriptions,
            "contact_tags": detail_context.contact_tags,
            "verification_badge": detail_context.verification_badge,
            "validation_badge": detail_context.validation_badge,
            "subscription_badge": detail_context.subscription_badge,
            "sendability": detail_context.sendability,
            "metrics": detail_context.metrics,
            "recent_activity": detail_context.recent_activity,
            "campaign_history": campaign_history,
            "transactional_history": transactional_history,
            "transactional_rows": transactional_rows,
            "events": events,
            "event_rows": event_rows,
            "audit_rows": latest_audits_for(contact),
            "contact_state_form": ContactStateForm(
                initial={
                    "verified_state": "verified" if contact.verified_at else "unverified",
                    "email_validation_status": contact.email_validation_status,
                    "email_validation_reason": contact.email_validation_reason,
                    "global_unsubscribed": contact.global_unsubscribed_at is not None,
                    "hard_bounced": contact.hard_bounced_at is not None,
                    "complained": contact.complained_at is not None,
                }
            ),
            "subscription_form": ContactSubscriptionForm(active_client=active_client),
            "tag_add_form": ContactTagAddForm(active_client=active_client),
            "tag_remove_form": ContactTagRemoveForm(contact=contact, active_client=active_client),
            "pagination_querystring": pagination_querystring(request),
        },
    )


@staff_member_required
@require_POST
def contact_state_update(request, contact_email):
    contact = get_contact_by_email_or_404(contact_email)
    form = ContactStateForm(request.POST)
    if form.is_valid():
        update_contact_state(
            actor=request.user,
            contact=contact,
            verified_state=form.cleaned_data["verified_state"],
            validation_status=form.cleaned_data["email_validation_status"],
            validation_reason=form.cleaned_data["email_validation_reason"],
            suppression_flags={
                "global_unsubscribed": form.cleaned_data["global_unsubscribed"],
                "hard_bounced": form.cleaned_data["hard_bounced"],
                "complained": form.cleaned_data["complained"],
            },
        )
        messages.success(request, "Contact state updated.")
    else:
        messages.error(request, "Contact state was not updated; check the form values.")
    return redirect("mailing:contact_detail", contact_email=contact.normalized_email)


@staff_member_required
@require_POST
def contact_subscription_update(request, contact_email):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    contact = get_contact_by_email_or_404(contact_email)
    form = ContactSubscriptionForm(request.POST, active_client=active_client)
    if form.is_valid():
        update_subscription(
            actor=request.user,
            contact=contact,
            audience=form.cleaned_data["audience"],
            client=form.cleaned_data["client"],
            status=form.cleaned_data["status"],
            unsubscribe_reason=form.cleaned_data["unsubscribe_reason"],
            verified=form.cleaned_data["verified"],
        )
        messages.success(request, "Subscription updated.")
    else:
        messages.error(request, "Subscription was not updated; check the form values.")
    return redirect("mailing:contact_detail", contact_email=contact.normalized_email)


@staff_member_required
@require_POST
def contact_tag_add(request, contact_email):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    contact = get_contact_by_email_or_404(contact_email)
    form = ContactTagAddForm(request.POST, active_client=active_client)
    if form.is_valid():
        add_contact_tag(
            actor=request.user,
            contact=contact,
            audience=form.cleaned_data["audience"],
            tag=form.cleaned_data["tag"],
            name=form.cleaned_data["new_tag_name"],
            slug=form.cleaned_data["new_tag_slug"],
        )
        messages.success(request, "Tag added.")
    else:
        messages.error(request, "Tag was not added; check the form values.")
    return redirect("mailing:contact_detail", contact_email=contact.normalized_email)


@staff_member_required
@require_POST
def contact_tag_remove(request, contact_email):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    contact = get_contact_by_email_or_404(contact_email)
    form = ContactTagRemoveForm(request.POST, contact=contact, active_client=active_client)
    if form.is_valid():
        remove_contact_tag(actor=request.user, contact=contact, tag=form.cleaned_data["membership"].tag)
        messages.success(request, "Tag removed.")
    else:
        messages.error(request, "Tag was not removed; check the form values.")
    return redirect("mailing:contact_detail", contact_email=contact.normalized_email)


@staff_member_required
def audience_list(request):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    audiences = paginate(request, audience_queryset(active_client), per_page=25)
    return render(
        request,
        "mailing/operator/audience_list.html",
        {
            "active_client": active_client,
            "audiences": audiences,
            "audience_rows": audience_list_rows(audiences.object_list, active_client),
            "pagination_querystring": pagination_querystring(request),
        },
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def audience_create(request):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    form = AudienceForm(request.POST or None, active_client=active_client)
    if request.method == "POST" and form.is_valid():
        audience = create_or_update_audience(actor=request.user, **form.cleaned_data)
        messages.success(request, "Audience created.")
        return redirect("mailing:audience_detail", audience_id=audience.id)
    return render(
        request, "mailing/operator/audience_form.html", {"form": form, "mode": "create", "active_client": active_client}
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def audience_edit(request, audience_id):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    audience = get_object_or_404(audience_detail_queryset(), pk=audience_id, organization=active_client.organization)
    form = AudienceForm(request.POST or None, instance=audience, active_client=active_client)
    if request.method == "POST" and form.is_valid():
        audience = create_or_update_audience(actor=request.user, audience=audience, **form.cleaned_data)
        messages.success(request, "Audience updated.")
        return redirect("mailing:audience_detail", audience_id=audience.id)
    return render(
        request,
        "mailing/operator/audience_form.html",
        {"form": form, "mode": "edit", "audience": audience, "active_client": active_client},
    )


@staff_member_required
def audience_detail(request, audience_id):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    audience = get_object_or_404(audience_detail_queryset(), pk=audience_id, organization=active_client.organization)
    filters = parse_contact_explorer_filters(
        request.GET,
        forced_audience_id=audience.id,
        forced_client_id=active_client.id,
    )
    members = paginate(request, contact_explorer_queryset(filters), per_page=25)
    member_rows = contact_result_rows(members.object_list, audience=audience, client=active_client)
    campaigns = paginate(request, audience_campaign_history(audience, active_client), per_page=10)
    event_type = request.GET.get("event_type", "")
    events = paginate(request, audience_recent_events(audience, event_type, active_client), per_page=25)
    event_rows = [
        {"event": event, "context": event_context(event), "metadata_summary": metadata_summary(event.metadata)}
        for event in events.object_list
    ]
    return render(
        request,
        "mailing/operator/audience_detail.html",
        {
            "audience": audience,
            "active_client": active_client,
            "summary": audience_summary(audience, active_client),
            "breakdowns": audience_breakdowns(audience, active_client),
            "filters": filters,
            "options": contact_explorer_options(active_client),
            "members": members,
            "member_rows": member_rows,
            "campaigns": campaigns,
            "events": events,
            "event_rows": event_rows,
            "tag_form": TagForm(audience=audience),
            "audit_rows": latest_audits_for(audience),
            "event_type": event_type,
            "event_type_options": choices_from_text_choices(EmailEventType),
            "pagination_querystring": pagination_querystring(request),
        },
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def tag_create(request, audience_id):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    audience = get_object_or_404(audience_detail_queryset(), pk=audience_id, organization=active_client.organization)
    form = TagForm(request.POST or None, audience=audience)
    if request.method == "POST" and form.is_valid():
        tag = create_or_update_tag(actor=request.user, audience=audience, **form.cleaned_data)
        messages.success(request, "Tag created.")
        return redirect("mailing:tag_detail", tag_id=tag.id)
    return render(request, "mailing/operator/tag_form.html", {"form": form, "audience": audience, "mode": "create"})


@staff_member_required
def tag_detail(request, tag_id):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    tag = get_object_or_404(
        Tag.objects.select_related("audience", "audience__organization"),
        pk=tag_id,
        audience__organization=active_client.organization,
    )
    return render(
        request,
        "mailing/operator/tag_detail.html",
        {
            "tag": tag,
            "membership_count": tag.contact_tags.count(),
            "audit_rows": latest_audits_for(tag),
        },
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def tag_edit(request, tag_id):
    active_client = require_active_client(request)
    if active_client is None:
        return redirect("mailing:dashboard")
    tag = get_object_or_404(
        Tag.objects.select_related("audience"), pk=tag_id, audience__organization=active_client.organization
    )
    form = TagForm(request.POST or None, instance=tag, audience=tag.audience)
    if request.method == "POST" and form.is_valid():
        tag = create_or_update_tag(actor=request.user, tag=tag, audience=tag.audience, **form.cleaned_data)
        messages.success(request, "Tag updated.")
        return redirect("mailing:tag_detail", tag_id=tag.id)
    return render(
        request, "mailing/operator/tag_form.html", {"form": form, "tag": tag, "audience": tag.audience, "mode": "edit"}
    )


@staff_member_required
def client_list(request):
    clients = paginate(
        request,
        Client.objects.select_related("organization")
        .annotate(
            active_key_count=Count("api_keys", filter=Q(api_keys__revoked_at__isnull=True), distinct=True),
            revoked_key_count=Count("api_keys", filter=Q(api_keys__revoked_at__isnull=False), distinct=True),
            latest_key_used_at=Max("api_keys__last_used_at"),
        )
        .order_by("organization__slug", "slug"),
        per_page=25,
    )
    return render(
        request,
        "mailing/operator/client_list.html",
        {"clients": clients, "pagination_querystring": pagination_querystring(request)},
    )


@staff_member_required
@require_POST
def client_select(request):
    client_id = request.POST.get("client_id")
    if client_id:
        client = get_object_or_404(Client, pk=client_id)
        request.session[ACTIVE_CLIENT_SESSION_KEY] = client.id
        messages.success(request, f"{client.name} is now the active client.")
    else:
        request.session.pop(ACTIVE_CLIENT_SESSION_KEY, None)
        messages.info(request, "Active client cleared.")
    return redirect(scoped_redirect_url(request))


@staff_member_required
@require_http_methods(["GET", "POST"])
def client_create(request):
    form = ClientForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        client = create_or_update_client(actor=request.user, **form.cleaned_data)
        request.session[ACTIVE_CLIENT_SESSION_KEY] = client.id
        messages.success(request, "Client created.")
        return redirect("mailing:client_detail", client_id=client.id)
    return render(request, "mailing/operator/client_form.html", {"form": form, "mode": "create"})


@staff_member_required
def client_detail(request, client_id):
    client = get_object_or_404(Client.objects.select_related("organization"), pk=client_id)
    raw_api_key_context = None
    session_raw_api_key = request.session.get("operator_raw_api_key")
    if session_raw_api_key and session_raw_api_key.get("client_id") == client.id:
        raw_api_key_context = request.session.pop("operator_raw_api_key")
    key_form = ClientApiKeyForm(client=client)
    api_keys = client_api_keys_for_detail(client)
    cmp_callbacks = (
        CmpCallback.objects.filter(client=client)
        .select_related("contact", "email_event")
        .order_by("-created_at", "-id")[:10]
    )
    mailchimp_syncs = (
        MailchimpSync.objects.filter(client=client)
        .select_related("contact")
        .order_by("-created_at", "-id")[:10]
    )
    return render(
        request,
        "mailing/operator/client_detail.html",
        {
            "client": client,
            "api_keys": api_keys,
            "active_key_count": sum(1 for api_key in api_keys if api_key.revoked_at is None),
            "revoked_key_count": sum(1 for api_key in api_keys if api_key.revoked_at is not None),
            "key_form": key_form,
            "raw_api_key_context": raw_api_key_context,
            "cmp_callbacks": cmp_callbacks,
            "mailchimp_status": mailchimp_status_payload(client),
            "mailchimp_syncs": mailchimp_syncs,
            "mailchimp_audiences": _mailchimp_tag_mapping_editors(client),
            "audit_rows": latest_audits_for(client),
        },
    )


def _mailchimp_tag_mapping_editors(client):
    """One prefilled `list_key = tag` textarea per audience in the client's org."""
    mappings_by_audience = {}
    for mapping in MailchimpTagMapping.objects.filter(client=client).select_related("audience"):
        mappings_by_audience.setdefault(mapping.audience_id, []).append(mapping)
    editors = []
    for audience in Audience.objects.filter(organization=client.organization).order_by("slug"):
        rows = mappings_by_audience.get(audience.id, [])
        text = "\n".join(
            f"{row.list_key} = {row.tag}" + ("" if row.enabled else "  (disabled)")
            for row in sorted(rows, key=lambda row: (row.list_key, row.tag))
        )
        editors.append({"audience": audience, "mappings_text": text, "count": len(rows)})
    return editors


@staff_member_required
@require_POST
def client_mailchimp_tag_mappings(request, client_id):
    client = get_object_or_404(Client.objects.select_related("organization"), pk=client_id)
    audience = get_object_or_404(
        Audience, pk=request.POST.get("audience_id"), organization=client.organization
    )
    mappings = []
    for raw_line in (request.POST.get("mappings") or "").splitlines():
        enabled = "(disabled)" not in raw_line
        line = raw_line.split("(disabled)", 1)[0].strip()
        if not line or "=" not in line:
            continue
        list_key, tag = line.split("=", 1)
        list_key, tag = list_key.strip(), tag.strip()
        if list_key and tag:
            mappings.append({"list_key": list_key, "tag": tag, "enabled": enabled})
    try:
        reconcile_tag_mappings_for_client(audience, {"mappings": mappings}, client)
        messages.success(request, f"Mailchimp tag mappings saved for {audience.slug}.")
    except ApiValidationError as exc:
        messages.error(request, f"Could not save tag mappings: {exc.errors}")
    return redirect("mailing:client_detail", client_id=client.id)


@staff_member_required
@require_http_methods(["GET", "POST"])
def client_edit(request, client_id):
    client = get_object_or_404(Client.objects.select_related("organization"), pk=client_id)
    form = ClientForm(request.POST or None, instance=client)
    if request.method == "POST" and form.is_valid():
        client = create_or_update_client(actor=request.user, client=client, **form.cleaned_data)
        messages.success(request, "Client updated.")
        return redirect("mailing:client_detail", client_id=client.id)
    return render(request, "mailing/operator/client_form.html", {"form": form, "mode": "edit", "client": client})


@staff_member_required
@require_POST
def client_api_key_create(request, client_id):
    client = get_object_or_404(Client, pk=client_id)
    form = ClientApiKeyForm(request.POST, client=client)
    if not form.is_valid():
        return render(
            request,
            "mailing/operator/client_detail.html",
            {
                "client": client,
                "api_keys": client_api_keys_for_detail(client),
                "active_key_count": client.active_api_key_count,
                "revoked_key_count": client.api_keys.filter(revoked_at__isnull=False).count(),
                "key_form": form,
                "raw_api_key_context": None,
                "audit_rows": latest_audits_for(client),
            },
            status=400,
        )
    api_key, raw_key = create_api_key(actor=request.user, client=client, **form.cleaned_data)
    request.session["operator_raw_api_key"] = {
        "raw_key": raw_key,
        "name": api_key.name,
        "prefix": api_key.display_prefix,
        "client_id": client.id,
    }
    messages.success(request, "API key generated. Copy it now; it will not be shown again.")
    return redirect("mailing:client_detail", client_id=client.id)


@staff_member_required
@require_POST
def client_api_key_revoke(request, client_id, key_id):
    client = get_object_or_404(Client, pk=client_id)
    api_key = get_object_or_404(ClientApiKey.objects.select_related("client"), pk=key_id, client=client)
    if revoke_api_key(actor=request.user, api_key=api_key):
        messages.success(request, "API key revoked.")
    else:
        messages.info(request, "API key was already revoked.")
    return redirect("mailing:client_detail", client_id=client.id)


def transparent_gif_response(*, status=200):
    response = HttpResponse(TRANSPARENT_GIF, status=status, content_type="image/gif")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Content-Length"] = str(len(TRANSPARENT_GIF))
    return response


@require_GET
def tracking_open(request, tracking_token):
    recipient = record_open(tracking_token)
    if recipient is None:
        return transparent_gif_response(status=404)
    return transparent_gif_response()


@require_GET
def tracking_click(request, tracking_token):
    destination_url = request.GET.get("u", "")
    recipient = record_click(tracking_token, destination_url)
    if recipient is None:
        return JsonResponse({"error": {"code": "invalid_tracking_redirect"}}, status=400)
    return redirect(destination_url)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def public_unsubscribe(request, unsubscribe_token):
    recipient = get_recipient_by_unsubscribe_token(unsubscribe_token)
    if recipient is None:
        return render(request, "mailing/unsubscribe.html", status=404, context={"invalid": True})

    if request.method == "POST":
        recipient = apply_unsubscribe(unsubscribe_token, request.POST.get("scope", ""))
        if recipient is None:
            return render(
                request,
                "mailing/unsubscribe.html",
                status=400,
                context={"recipient": get_recipient_by_unsubscribe_token(unsubscribe_token), "invalid_scope": True},
            )
        return render(request, "mailing/unsubscribe.html", context={"recipient": recipient, "confirmed": True})

    return render(request, "mailing/unsubscribe.html", context={"recipient": recipient})


def authenticate_api_request(request):
    auth_result = authenticate_bearer_token(request.headers.get("Authorization"))
    if auth_result.is_authenticated:
        return auth_result.client, None

    return None, JsonResponse(
        {
            "error": {
                "code": auth_result.error,
                "message": "Authentication credentials were not accepted.",
            }
        },
        status=auth_result.status_code,
    )


def json_request_body(request):
    try:
        if not request.body:
            return {}
        parsed = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ApiValidationError({"body": "invalid_json"}) from exc

    if not isinstance(parsed, dict):
        raise ApiValidationError({"body": "must_be_object"})
    return parsed


def validation_error_response(exc):
    return JsonResponse(
        {
            "error": {
                "code": "validation_error",
                "fields": exc.errors,
            }
        },
        status=exc.status_code,
    )


def method_not_allowed_response(allowed_methods):
    return JsonResponse(
        {
            "error": {
                "code": "method_not_allowed",
                "allowed_methods": allowed_methods,
            }
        },
        status=405,
    )


def api_request_data(request):
    if request.body:
        return json_request_body(request)
    return request.GET


@csrf_exempt
def api_contacts(request):
    if request.method not in {"GET", "POST"}:
        return method_not_allowed_response(["GET", "POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        if request.method == "GET":
            payload = export_contacts_for_client(request.GET, client)
        else:
            payload = upsert_contact_for_client(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


def api_contacts_csv(request):
    if request.method != "GET":
        return method_not_allowed_response(["GET"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        csv_body = export_contacts_csv_for_client(request.GET, client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    response = HttpResponse(csv_body, content_type="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = 'attachment; filename="contacts.csv"'
    return response


@csrf_exempt
def api_contact_imports(request):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = bulk_import_contacts_for_client(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_contact_imports_csv(request):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        data = request.POST.copy()
        data.setdefault("dry_run", "false")
        if "file" in request.FILES:
            csv_body = request.FILES["file"].read().decode("utf-8-sig")
        else:
            payload = json_request_body(request) if request.content_type.startswith("application/json") else {}
            data.update(payload)
            csv_body = payload.get("csv", "")
        if not csv_body:
            raise ApiValidationError({"csv": "required"})
        payload = csv_import_contacts_for_client(csv_body, data, client)
    except UnicodeDecodeError:
        return validation_error_response(ApiValidationError({"csv": "must_be_utf8"}))
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


def api_contact_status(request):
    if request.method != "GET":
        return method_not_allowed_response(["GET"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = get_contact_status_for_client(request.GET, client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_contact_erase(request):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = erase_contact_for_client(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_subscribe(request):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = subscribe_for_client(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_unsubscribe(request):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = unsubscribe_for_client(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_contact_preferences(request):
    if request.method not in {"GET", "PUT"}:
        return method_not_allowed_response(["GET", "PUT"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        if request.method == "GET":
            payload = get_contact_preferences_for_client(request.GET, client)
        else:
            payload = update_contact_preferences_for_client(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_contact_tags(request, contact_id):
    if request.method != "PUT":
        return method_not_allowed_response(["PUT"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = replace_contact_tags_for_client(contact_id, json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_contact_tag(request, contact_id, tag_slug):
    if request.method not in {"POST", "DELETE"}:
        return method_not_allowed_response(["POST", "DELETE"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        data = api_request_data(request)
        if request.method == "POST":
            payload = add_contact_tag_for_client(contact_id, tag_slug, data, client)
        else:
            payload = remove_contact_tag_for_client(contact_id, tag_slug, data, client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_contact_verification(request, contact_id):
    if request.method != "PATCH":
        return method_not_allowed_response(["PATCH"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = update_contact_verification_for_client(contact_id, json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_contact_validation(request, contact_id):
    if request.method != "PATCH":
        return method_not_allowed_response(["PATCH"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = update_contact_validation_for_client(contact_id, json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_contact_suppression(request, contact_id):
    if request.method != "PATCH":
        return method_not_allowed_response(["PATCH"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = update_contact_suppression_for_client(contact_id, json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


def api_contact_history(request, contact_id):
    if request.method != "GET":
        return method_not_allowed_response(["GET"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = get_contact_history_for_client(contact_id, request.GET, client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


def api_transactional_message_status(request, message_id):
    if request.method != "GET":
        return method_not_allowed_response(["GET"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = get_transactional_message_status_for_client(
            message_id,
            client,
        )
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_recipient_list(request, list_key):
    if request.method not in {"GET", "PUT"}:
        return method_not_allowed_response(["GET", "PUT"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        if request.method == "GET":
            payload = get_recipient_list_for_client(list_key, request.GET, client)
        else:
            payload = upsert_recipient_list_for_client(list_key, json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_recipient_list_member(request, list_key, source_object_key):
    if request.method not in {"DELETE", "PUT"}:
        return method_not_allowed_response(["DELETE", "PUT"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        if request.method == "PUT":
            payload = upsert_recipient_list_member_for_client(
                list_key,
                source_object_key,
                json_request_body(request),
                client,
            )
        else:
            payload = remove_recipient_list_member_for_client(
                list_key,
                source_object_key,
                json_request_body(request),
                client,
            )
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_recipient_list_members(request, list_key):
    if request.method != "GET":
        return method_not_allowed_response(["GET"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = get_recipient_list_members_for_client(
            list_key,
            request.GET,
            client,
        )
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_recipient_list_bulk_upsert(request, list_key):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = bulk_upsert_recipient_list_members_for_client(list_key, json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_recipient_list_reconcile(request, list_key):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = reconcile_recipient_list_for_client(list_key, json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_recipient_list_imports(request, list_key):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = create_recipient_list_import_job_for_client(list_key, json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=202 if payload.get("created") else 200)


@csrf_exempt
def api_recipient_list_import(request, list_key, job_id):
    if request.method != "GET":
        return method_not_allowed_response(["GET"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = get_recipient_list_import_job_for_client(list_key, job_id, request.GET, client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_recipient_list_transactional_send(request, list_key):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = send_transactional_email_to_recipient_list_for_client(
            list_key,
            json_request_body(request),
            client,
        )
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=202)


@csrf_exempt
def api_transient_recipient_list_transactional_send(request):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = send_transactional_email_to_transient_recipient_list_for_client(
            json_request_body(request),
            client,
        )
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=202)


@csrf_exempt
def api_transactional_template(request, template_key):
    if request.method not in {"GET", "PUT"}:
        return method_not_allowed_response(["GET", "PUT"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        if request.method == "GET":
            payload = get_transactional_template_for_client(template_key, client)
        else:
            payload = upsert_transactional_template_for_client(
                template_key,
                json_request_body(request),
                client,
            )
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
@require_POST
def api_transactional_send(request):
    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = send_transactional_email_for_client(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)
    except TransactionalSendRejected as exc:
        return JsonResponse(exc.payload, status=exc.status_code)

    return JsonResponse(payload, status=202)


@csrf_exempt
def api_campaign(request, external_key):
    if request.method not in {"GET", "PUT"}:
        return method_not_allowed_response(["GET", "PUT"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        if request.method == "GET":
            payload = get_campaign_for_client(
                external_key,
                request.GET,
                client,
            )
            status = 200
        else:
            payload = upsert_campaign_for_client(
                external_key,
                json_request_body(request),
                client,
            )
            status = 201 if payload.get("created") else 200
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=status)


@csrf_exempt
def api_campaign_queue(request, external_key):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = queue_campaign_for_client(
            external_key,
            json_request_body(request),
            client,
        )
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=202)


@csrf_exempt
def api_campaign_cancel(request, external_key):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = cancel_campaign_for_client(
            external_key,
            json_request_body(request),
            client,
        )
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_campaign_preview(request, external_key):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = preview_campaign_for_client(
            external_key,
            json_request_body(request),
            client,
        )
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_campaign_test_send(request, external_key):
    if request.method != "POST":
        return method_not_allowed_response(["POST"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = test_send_campaign_for_client(
            external_key,
            json_request_body(request),
            client,
        )
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=202)


@csrf_exempt
def api_client_senders(request):
    if request.method not in {"GET", "PUT"}:
        return method_not_allowed_response(["GET", "PUT"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        if request.method == "GET":
            payload = get_client_sender_policy_for_client(client)
        else:
            payload = update_client_sender_policy_for_client(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_client_mailchimp(request):
    # Set-only: a client can write its Mailchimp key/audience/enabled state but
    # the stored key is never returned.
    if request.method != "PUT":
        return method_not_allowed_response(["PUT"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = update_mailchimp_config_for_client(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
def api_client_mailchimp_tag_mappings(request):
    if request.method != "PUT":
        return method_not_allowed_response(["PUT"])

    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        data = json_request_body(request)
        scope = validate_contact_scope(data, client, require_email=False, require_client=False)
        payload = reconcile_tag_mappings_for_client(scope.audience, data, client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
@require_POST
def ses_webhook(request):
    try:
        payload = json_request_body(request)
        result = ingest_sns_webhook(payload)
    except SnsSignatureError as exc:
        return JsonResponse({"error": {"code": "invalid_sns_signature", "message": str(exc)}}, status=exc.status_code)
    except SesWebhookError as exc:
        return JsonResponse({"error": {"code": "invalid_sns_message", "message": str(exc)}}, status=exc.status_code)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(
        {
            "status": "ok",
            "type": result.message_type,
            "enqueued": result.enqueued,
            "confirmed": result.confirmed,
        },
        status=200,
    )
