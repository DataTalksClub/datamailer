import json

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from mailing.services.api import (
    ApiValidationError,
    get_contact_status_for_client,
    subscribe_for_client,
    unsubscribe_for_client,
    upsert_contact_for_client,
)
from mailing.services.auth import authenticate_bearer_token
from mailing.services.transactional import TransactionalSendRejected, send_transactional_email_for_client


def health(request):
    return JsonResponse({"status": "ok"})


def dashboard(request):
    return render(request, "mailing/dashboard.html")


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
