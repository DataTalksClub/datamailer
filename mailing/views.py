import json

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

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
from mailing.models import Campaign, CampaignStatus, Client, ClientApiKey, EmailEventType, Tag
from mailing.services.api import (
    ApiValidationError,
    add_contact_tag_for_client,
    get_contact_history_for_client,
    get_contact_status_for_client,
    get_transactional_message_status_for_client,
    get_transactional_template_for_client,
    remove_contact_tag_for_client,
    replace_contact_tags_for_client,
    subscribe_for_client,
    unsubscribe_for_client,
    update_contact_suppression_for_client,
    update_contact_validation_for_client,
    update_contact_verification_for_client,
    upsert_contact_for_client,
    upsert_transactional_template_for_client,
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
    event_context,
    metadata_summary,
    parse_contact_explorer_filters,
)
from mailing.services.ses_webhooks import SesWebhookError, SnsSignatureError, ingest_sns_webhook
from mailing.services.tokens import get_recipient_by_unsubscribe_token
from mailing.services.tracking import TRANSPARENT_GIF, apply_unsubscribe, record_click, record_open
from mailing.services.transactional import TransactionalSendRejected, send_transactional_email_for_client
from mailing.services.transactional_catalog import (
    catalog_context,
    filter_transactional_templates,
    recent_message_rows,
    template_catalog_rows,
    transactional_template_queryset,
)


def health(request):
    return JsonResponse({"status": "ok"})


@staff_member_required
def dashboard(request):
    return render(request, "mailing/dashboard.html", {"dashboard": dashboard_context()})


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
def template_catalog(request):
    client_id = request.GET.get("client", "")
    templates = paginate(
        request,
        filter_transactional_templates(transactional_template_queryset(), client_id),
        per_page=25,
    )
    clients = Client.objects.filter(email_templates__is_transactional=True).distinct().order_by(
        "organization__slug",
        "slug",
    )
    return render(
        request,
        "mailing/operator/template_catalog.html",
        {
            "templates": templates,
            "template_rows": template_catalog_rows(templates.object_list),
            "clients": clients,
            "active_client_id": int(client_id) if client_id.isdigit() else "",
            "pagination_querystring": pagination_querystring(request),
        },
    )


@staff_member_required
def template_detail(request, template_id):
    template = get_object_or_404(transactional_template_queryset(), pk=template_id)
    recent_messages = template.transactional_messages.select_related("contact").order_by("-created_at", "-id")[:10]
    return render(
        request,
        "mailing/operator/template_detail.html",
        catalog_context(template) | {"recent_message_rows": recent_message_rows(recent_messages)},
    )


@staff_member_required
def campaign_list(request):
    campaigns = paginate(request, campaign_queryset(), per_page=25)
    return render(
        request,
        "mailing/operator/campaign_list.html",
        {
            "campaigns": campaigns,
            "campaign_rows": campaign_list_rows(campaigns.object_list),
            "pagination_querystring": pagination_querystring(request),
        },
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def campaign_create(request):
    form = CampaignForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        campaign = form.save()
        messages.success(request, "Campaign draft created.")
        return redirect("mailing:campaign_detail", campaign_id=campaign.id)

    return render(
        request,
        "mailing/operator/campaign_form.html",
        {"form": form, "mode": "create", "campaign": None},
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def campaign_edit(request, campaign_id):
    campaign = get_object_or_404(Campaign.objects.select_related("client", "audience"), pk=campaign_id)
    form = CampaignForm(request.POST or None, instance=campaign)
    if request.method == "POST" and form.is_valid():
        campaign = form.save()
        messages.success(request, "Campaign draft updated.")
        return redirect("mailing:campaign_detail", campaign_id=campaign.id)

    return render(
        request,
        "mailing/operator/campaign_form.html",
        {"form": form, "mode": "edit", "campaign": campaign},
    )


@staff_member_required
def campaign_detail(request, campaign_id):
    campaign = get_object_or_404(
        Campaign.objects.select_related("client", "audience", "audience__organization"),
        pk=campaign_id,
    )
    active_filter = request.GET.get("filter", "")
    recipients = paginate(request, campaign_recipient_queryset(campaign, active_filter), per_page=50)
    estimate = estimate_campaign_recipients(campaign) if campaign.status == CampaignStatus.DRAFT else None
    events = campaign_recent_events(campaign)[:10]
    event_rows = [
        {"event": event, "metadata_summary": metadata_summary(event.metadata)}
        for event in events
    ]
    return render(
        request,
        "mailing/operator/campaign_detail.html",
        {
            "campaign": campaign,
            "campaign_badge": campaign_status_badge(campaign.status),
            "can_edit": campaign.status == CampaignStatus.DRAFT,
            "can_queue": campaign.status == CampaignStatus.DRAFT,
            "estimate": estimate,
            "stats": campaign_stats(campaign),
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
    campaign = get_object_or_404(Campaign, pk=campaign_id)
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
    filters = parse_contact_explorer_filters(request.GET)
    contacts = paginate(request, contact_explorer_queryset(filters), per_page=25) if filters.has_filters else None
    rows = contact_result_rows(contacts.object_list) if contacts is not None else []
    return render(
        request,
        "mailing/operator/contact_search.html",
        {
            "filters": filters,
            "options": contact_explorer_options(),
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
    contact = get_contact_by_email_or_404(contact_email)
    detail_context = contact_detail_context(contact)
    events = paginate(request, contact_event_timeline(contact), per_page=50)
    event_rows = [
        {"event": event, "context": event_context(event), "metadata_summary": metadata_summary(event.metadata)}
        for event in events.object_list
    ]
    campaign_history = paginate(request, contact_campaign_history(contact), per_page=25)
    transactional_history = paginate(request, contact_transactional_history(contact), per_page=25)
    transactional_rows = [
        {"message": message, "metadata_summary": metadata_summary(message.metadata)}
        for message in transactional_history.object_list
    ]
    return render(
        request,
        "mailing/operator/contact_detail.html",
        {
            "contact": contact,
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
            "subscription_form": ContactSubscriptionForm(),
            "tag_add_form": ContactTagAddForm(),
            "tag_remove_form": ContactTagRemoveForm(contact=contact),
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
    contact = get_contact_by_email_or_404(contact_email)
    form = ContactSubscriptionForm(request.POST)
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
    contact = get_contact_by_email_or_404(contact_email)
    form = ContactTagAddForm(request.POST)
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
    contact = get_contact_by_email_or_404(contact_email)
    form = ContactTagRemoveForm(request.POST, contact=contact)
    if form.is_valid():
        remove_contact_tag(actor=request.user, contact=contact, tag=form.cleaned_data["membership"].tag)
        messages.success(request, "Tag removed.")
    else:
        messages.error(request, "Tag was not removed; check the form values.")
    return redirect("mailing:contact_detail", contact_email=contact.normalized_email)


@staff_member_required
def audience_list(request):
    audiences = paginate(request, audience_queryset(), per_page=25)
    return render(
        request,
        "mailing/operator/audience_list.html",
        {
            "audiences": audiences,
            "audience_rows": audience_list_rows(audiences.object_list),
            "pagination_querystring": pagination_querystring(request),
        },
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def audience_create(request):
    form = AudienceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        audience = create_or_update_audience(actor=request.user, **form.cleaned_data)
        messages.success(request, "Audience created.")
        return redirect("mailing:audience_detail", audience_id=audience.id)
    return render(request, "mailing/operator/audience_form.html", {"form": form, "mode": "create"})


@staff_member_required
@require_http_methods(["GET", "POST"])
def audience_edit(request, audience_id):
    audience = get_object_or_404(audience_detail_queryset(), pk=audience_id)
    form = AudienceForm(request.POST or None, instance=audience)
    if request.method == "POST" and form.is_valid():
        audience = create_or_update_audience(actor=request.user, audience=audience, **form.cleaned_data)
        messages.success(request, "Audience updated.")
        return redirect("mailing:audience_detail", audience_id=audience.id)
    return render(request, "mailing/operator/audience_form.html", {"form": form, "mode": "edit", "audience": audience})


@staff_member_required
def audience_detail(request, audience_id):
    audience = get_object_or_404(audience_detail_queryset(), pk=audience_id)
    filters = parse_contact_explorer_filters(request.GET, forced_audience_id=audience.id)
    members = paginate(request, contact_explorer_queryset(filters), per_page=25)
    member_rows = contact_result_rows(members.object_list, audience=audience)
    campaigns = paginate(request, audience_campaign_history(audience), per_page=10)
    event_type = request.GET.get("event_type", "")
    events = paginate(request, audience_recent_events(audience, event_type), per_page=25)
    event_rows = [
        {"event": event, "context": event_context(event), "metadata_summary": metadata_summary(event.metadata)}
        for event in events.object_list
    ]
    return render(
        request,
        "mailing/operator/audience_detail.html",
        {
            "audience": audience,
            "summary": audience_summary(audience),
            "breakdowns": audience_breakdowns(audience),
            "filters": filters,
            "options": contact_explorer_options(),
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
    audience = get_object_or_404(audience_detail_queryset(), pk=audience_id)
    form = TagForm(request.POST or None, audience=audience)
    if request.method == "POST" and form.is_valid():
        tag = create_or_update_tag(actor=request.user, audience=audience, **form.cleaned_data)
        messages.success(request, "Tag created.")
        return redirect("mailing:tag_detail", tag_id=tag.id)
    return render(request, "mailing/operator/tag_form.html", {"form": form, "audience": audience, "mode": "create"})


@staff_member_required
def tag_detail(request, tag_id):
    tag = get_object_or_404(Tag.objects.select_related("audience", "audience__organization"), pk=tag_id)
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
    tag = get_object_or_404(Tag.objects.select_related("audience"), pk=tag_id)
    form = TagForm(request.POST or None, instance=tag, audience=tag.audience)
    if request.method == "POST" and form.is_valid():
        tag = create_or_update_tag(actor=request.user, tag=tag, audience=tag.audience, **form.cleaned_data)
        messages.success(request, "Tag updated.")
        return redirect("mailing:tag_detail", tag_id=tag.id)
    return render(request, "mailing/operator/tag_form.html", {"form": form, "tag": tag, "audience": tag.audience, "mode": "edit"})


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
@require_http_methods(["GET", "POST"])
def client_create(request):
    form = ClientForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        client = create_or_update_client(actor=request.user, **form.cleaned_data)
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
            "audit_rows": latest_audits_for(client),
        },
    )


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
