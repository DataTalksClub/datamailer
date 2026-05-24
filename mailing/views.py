import json

from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from mailing.models import Campaign
from mailing.services.api import (
    ApiValidationError,
    get_contact_status_for_client,
    subscribe_for_client,
    unsubscribe_for_client,
    upsert_contact_for_client,
)
from mailing.services.auth import authenticate_bearer_token
from mailing.services.operator_ui import (
    RECIPIENT_FILTER_LABELS,
    campaign_queryset,
    campaign_recipient_queryset,
    campaign_stats,
    contact_campaign_history,
    contact_detail_queryset,
    contact_event_timeline,
    contact_search_queryset,
    contact_transactional_history,
    event_context,
    metadata_summary,
)
from mailing.services.ses_webhooks import SesWebhookError, SnsSignatureError, ingest_sns_webhook
from mailing.services.tokens import get_recipient_by_unsubscribe_token
from mailing.services.tracking import TRANSPARENT_GIF, apply_unsubscribe, record_click, record_open
from mailing.services.transactional import TransactionalSendRejected, send_transactional_email_for_client


def health(request):
    return JsonResponse({"status": "ok"})


def dashboard(request):
    return render(request, "mailing/dashboard.html")


def paginate(request, queryset, *, per_page):
    paginator = Paginator(queryset, per_page)
    return paginator.get_page(request.GET.get("page"))


def pagination_querystring(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


@staff_member_required
def operator_campaign_list(request):
    campaigns = paginate(request, campaign_queryset(), per_page=25)
    return render(
        request,
        "mailing/operator/campaign_list.html",
        {"campaigns": campaigns, "pagination_querystring": pagination_querystring(request)},
    )


@staff_member_required
def operator_campaign_detail(request, campaign_id):
    campaign = get_object_or_404(
        Campaign.objects.select_related("client", "audience", "audience__organization"),
        pk=campaign_id,
    )
    active_filter = request.GET.get("filter", "")
    recipients = paginate(request, campaign_recipient_queryset(campaign, active_filter), per_page=50)
    return render(
        request,
        "mailing/operator/campaign_detail.html",
        {
            "campaign": campaign,
            "stats": campaign_stats(campaign),
            "recipients": recipients,
            "recipient_filter_labels": RECIPIENT_FILTER_LABELS,
            "active_filter": active_filter if active_filter in RECIPIENT_FILTER_LABELS else "",
            "pagination_querystring": pagination_querystring(request),
        },
    )


@staff_member_required
def operator_contact_search(request):
    query = request.GET.get("q", "")
    contacts = paginate(request, contact_search_queryset(query), per_page=25) if query.strip() else None
    return render(
        request,
        "mailing/operator/contact_search.html",
        {"query": query, "contacts": contacts, "pagination_querystring": pagination_querystring(request)},
    )


@staff_member_required
def operator_contact_detail(request, contact_id):
    contact = get_object_or_404(contact_detail_queryset(), pk=contact_id)
    events = paginate(request, contact_event_timeline(contact), per_page=50)
    event_rows = [
        {"event": event, "context": event_context(event), "metadata_summary": metadata_summary(event.metadata)}
        for event in events.object_list
    ]
    campaign_history = paginate(request, contact_campaign_history(contact), per_page=25)
    transactional_history = paginate(request, contact_transactional_history(contact), per_page=25)
    return render(
        request,
        "mailing/operator/contact_detail.html",
        {
            "contact": contact,
            "subscriptions": contact.subscriptions.all(),
            "contact_tags": contact.contact_tags.all(),
            "campaign_history": campaign_history,
            "transactional_history": transactional_history,
            "events": events,
            "event_rows": event_rows,
            "pagination_querystring": pagination_querystring(request),
        },
    )


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


@csrf_exempt
@require_POST
def api_contacts(request):
    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = upsert_contact_for_client(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@require_GET
def api_contact_status(request):
    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = get_contact_status_for_client(request.GET, client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
@require_POST
def api_subscribe(request):
    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = subscribe_for_client(json_request_body(request), client)
    except ApiValidationError as exc:
        return validation_error_response(exc)

    return JsonResponse(payload, status=200)


@csrf_exempt
@require_POST
def api_unsubscribe(request):
    client, error_response = authenticate_api_request(request)
    if error_response:
        return error_response

    try:
        payload = unsubscribe_for_client(json_request_body(request), client)
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
