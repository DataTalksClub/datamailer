from copy import deepcopy

from django.urls import reverse

from mailing.models import EmailValidationStatus, SubscriptionStatus

API_DOC_PATHS = {
    "mailing:api_contacts": "/api/contacts",
    "mailing:api_contacts_csv": "/api/contacts.csv",
    "mailing:api_contact_imports": "/api/contacts/imports",
    "mailing:api_contact_imports_csv": "/api/contacts/imports/csv",
    "mailing:api_contact_status": "/api/contacts/status",
    "mailing:api_contact_tags": "/api/contacts/{contact_id}/tags",
    "mailing:api_contact_tag": "/api/contacts/{contact_id}/tags/{tag_slug}",
    "mailing:api_contact_verification": "/api/contacts/{contact_id}/verification",
    "mailing:api_contact_validation": "/api/contacts/{contact_id}/validation",
    "mailing:api_contact_suppression": "/api/contacts/{contact_id}/suppression",
    "mailing:api_contact_history": "/api/contacts/{contact_id}/history",
    "mailing:api_subscribe": "/api/subscriptions/subscribe",
    "mailing:api_unsubscribe": "/api/subscriptions/unsubscribe",
    "mailing:api_transactional_send": "/api/transactional/send",
    "mailing:tracking_open": "/t/o/{tracking_token}.gif",
    "mailing:tracking_click": "/t/c/{tracking_token}",
    "mailing:public_unsubscribe": "/unsubscribe/{unsubscribe_token}",
    "mailing:ses_webhook": "/webhooks/ses",
}


def endpoint_groups():
    return [
        {
            "name": "Contacts",
            "endpoints": [
                ("POST", "/api/contacts", "Upsert one contact in an audience/client scope."),
                ("GET", "/api/contacts/status", "Look up contact status for one scoped email."),
                ("GET", "/api/contacts", "List/export contacts as paginated JSON."),
                ("GET", "/api/contacts.csv", "Export contacts as CSV."),
            ],
        },
        {
            "name": "Subscriptions and Tags",
            "endpoints": [
                ("POST", "/api/subscriptions/subscribe", "Subscribe one scoped contact."),
                ("POST", "/api/subscriptions/unsubscribe", "Unsubscribe one scoped contact."),
                ("PUT", "/api/contacts/{contact_id}/tags", "Replace one contact's scoped tags."),
                ("POST", "/api/contacts/{contact_id}/tags/{tag_slug}", "Add one scoped tag."),
                ("DELETE", "/api/contacts/{contact_id}/tags/{tag_slug}", "Remove one scoped tag."),
            ],
        },
        {
            "name": "State and History",
            "endpoints": [
                ("PATCH", "/api/contacts/{contact_id}/verification", "Set verification state."),
                ("PATCH", "/api/contacts/{contact_id}/validation", "Set email validation state."),
                ("PATCH", "/api/contacts/{contact_id}/suppression", "Set suppression state."),
                ("GET", "/api/contacts/{contact_id}/history", "Return safe scoped send and event history."),
            ],
        },
        {
            "name": "Imports and Transactional",
            "endpoints": [
                ("POST", "/api/contacts/imports", "Bulk JSON import/upsert."),
                ("POST", "/api/contacts/imports/csv", "CSV upload/import."),
                ("POST", "/api/transactional/send", "Queue one transactional email."),
            ],
        },
        {
            "name": "Public and Provider",
            "endpoints": [
                ("GET", "/t/o/{tracking_token}.gif", "Open tracking pixel."),
                ("GET", "/t/c/{tracking_token}", "Click tracking redirect."),
                ("GET/POST", "/unsubscribe/{unsubscribe_token}", "Public unsubscribe form."),
                ("POST", "/webhooks/ses", "SES/SNS provider webhook ingress."),
            ],
        },
    ]


def build_openapi_spec(request=None):
    spec = deepcopy(OPENAPI_SPEC)
    if request is not None:
        spec["servers"] = [{"url": request.build_absolute_uri("/").rstrip("/")}]
    return spec


def route_path_map():
    return {
        API_DOC_PATHS["mailing:api_contacts"]: reverse("mailing:api_contacts"),
        API_DOC_PATHS["mailing:api_contacts_csv"]: reverse("mailing:api_contacts_csv"),
        API_DOC_PATHS["mailing:api_contact_imports"]: reverse("mailing:api_contact_imports"),
        API_DOC_PATHS["mailing:api_contact_imports_csv"]: reverse("mailing:api_contact_imports_csv"),
        API_DOC_PATHS["mailing:api_contact_status"]: reverse("mailing:api_contact_status"),
        API_DOC_PATHS["mailing:api_contact_tags"]: reverse("mailing:api_contact_tags", args=[123]),
        API_DOC_PATHS["mailing:api_contact_tag"]: reverse("mailing:api_contact_tag", args=[123, "newsletter"]),
        API_DOC_PATHS["mailing:api_contact_verification"]: reverse("mailing:api_contact_verification", args=[123]),
        API_DOC_PATHS["mailing:api_contact_validation"]: reverse("mailing:api_contact_validation", args=[123]),
        API_DOC_PATHS["mailing:api_contact_suppression"]: reverse("mailing:api_contact_suppression", args=[123]),
        API_DOC_PATHS["mailing:api_contact_history"]: reverse("mailing:api_contact_history", args=[123]),
        API_DOC_PATHS["mailing:api_subscribe"]: reverse("mailing:api_subscribe"),
        API_DOC_PATHS["mailing:api_unsubscribe"]: reverse("mailing:api_unsubscribe"),
        API_DOC_PATHS["mailing:api_transactional_send"]: reverse("mailing:api_transactional_send"),
        API_DOC_PATHS["mailing:tracking_open"]: reverse("mailing:tracking_open", args=["tracking"]),
        API_DOC_PATHS["mailing:tracking_click"]: reverse("mailing:tracking_click", args=["tracking"]),
        API_DOC_PATHS["mailing:public_unsubscribe"]: reverse("mailing:public_unsubscribe", args=["unsubscribe"]),
        API_DOC_PATHS["mailing:ses_webhook"]: reverse("mailing:ses_webhook"),
    }


def json_response(description="OK", schema_ref=None):
    content = {"application/json": {"schema": {"$ref": schema_ref}}} if schema_ref else {}
    return {"description": description, "content": content}


def csv_response(description="CSV file"):
    return {
        "description": description,
        "content": {"text/csv": {"schema": {"type": "string"}}},
    }


def json_body(schema_ref, *, required=True):
    return {
        "required": required,
        "content": {"application/json": {"schema": {"$ref": schema_ref}}},
    }


def bearer_responses(success, *, accepted=False):
    responses = {"200": success, "400": {"$ref": "#/components/responses/ValidationError"}}
    if accepted:
        responses = {"202": success, "400": {"$ref": "#/components/responses/ValidationError"}}
    responses["401"] = {"$ref": "#/components/responses/Unauthorized"}
    responses["403"] = {"$ref": "#/components/responses/Forbidden"}
    responses["405"] = {"$ref": "#/components/responses/MethodNotAllowed"}
    return responses


CONTACT_ID_PARAM = {"name": "contact_id", "in": "path", "required": True, "schema": {"type": "integer"}}
TAG_SLUG_PARAM = {"name": "tag_slug", "in": "path", "required": True, "schema": {"type": "string"}}
TRACKING_PARAM = {"name": "tracking_token", "in": "path", "required": True, "schema": {"type": "string"}}
UNSUBSCRIBE_PARAM = {"name": "unsubscribe_token", "in": "path", "required": True, "schema": {"type": "string"}}

SCOPE_QUERY_PARAMS = [
    {"name": "email", "in": "query", "required": True, "schema": {"type": "string", "format": "email"}},
    {"name": "audience", "in": "query", "required": True, "schema": {"type": "string"}},
    {"name": "client", "in": "query", "required": True, "schema": {"type": "string"}},
]

EXPORT_QUERY_PARAMS = [
    {"name": "audience", "in": "query", "required": True, "schema": {"type": "string"}},
    {"name": "client", "in": "query", "required": True, "schema": {"type": "string"}},
    {"name": "tags", "in": "query", "schema": {"type": "string", "description": "Comma-separated tag slugs."}},
    {"name": "subscription_status", "in": "query", "schema": {"$ref": "#/components/schemas/SubscriptionStatus"}},
    {"name": "verified", "in": "query", "schema": {"type": "boolean"}},
    {"name": "email_validation_status", "in": "query", "schema": {"$ref": "#/components/schemas/EmailValidationStatus"}},
    {
        "name": "suppression",
        "in": "query",
        "schema": {
            "type": "string",
            "enum": ["none", "any", "global_unsubscribed", "hard_bounced", "complained"],
        },
    },
    {"name": "updated_since", "in": "query", "schema": {"type": "string", "format": "date-time"}},
    {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1}},
    {"name": "cursor", "in": "query", "schema": {"type": "integer", "minimum": 1}},
    {"name": "offset", "in": "query", "schema": {"type": "integer", "minimum": 1}},
]

OPENAPI_SPEC = {
    "openapi": "3.1.0",
    "info": {
        "title": "Datamailer Native API",
        "version": "1.0.0",
        "description": "Local staff reference for implemented Datamailer endpoints. Client API routes use Bearer authentication with Datamailer client API keys. Transactional templates are planned for catalog management and may be provisioned externally for now.",
    },
    "servers": [{"url": "/"}],
    "tags": [
        {"name": "Contacts"},
        {"name": "Subscriptions"},
        {"name": "Tags"},
        {"name": "State"},
        {"name": "Imports"},
        {"name": "Transactional"},
        {"name": "Public"},
        {"name": "Provider"},
    ],
    "paths": {
        "/api/contacts": {
            "get": {
                "tags": ["Contacts"],
                "summary": "List contacts",
                "security": [{"BearerAuth": []}],
                "parameters": EXPORT_QUERY_PARAMS,
                "responses": bearer_responses(json_response("Contacts list", "#/components/schemas/ContactListResponse")),
            },
            "post": {
                "tags": ["Contacts"],
                "summary": "Upsert contact",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/ContactUpsertRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            },
        },
        "/api/contacts.csv": {
            "get": {
                "tags": ["Contacts"],
                "summary": "Export contacts CSV",
                "description": "Exports safe recreatable contact, subscription, tag, verification, validation, suppression, unsubscribe, and update timestamp columns. Secret hashes and delivery link tokens are never exported.",
                "security": [{"BearerAuth": []}],
                "parameters": EXPORT_QUERY_PARAMS,
                "responses": bearer_responses(csv_response()),
            }
        },
        "/api/contacts/imports": {
            "post": {
                "tags": ["Imports"],
                "summary": "Bulk import contacts",
                "description": "Imports are idempotent by normalized email plus audience/client scope. Invalid items are returned in partial errors while other items continue.",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/BulkImportRequest"),
                "responses": bearer_responses(json_response("Import result", "#/components/schemas/ImportResult")),
            }
        },
        "/api/contacts/imports/csv": {
            "post": {
                "tags": ["Imports"],
                "summary": "Import contacts CSV",
                "description": "Accepts CSV text in JSON or an uploaded file using the export column semantics. Invalid rows are reported without aborting valid rows.",
                "security": [{"BearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {"schema": {"$ref": "#/components/schemas/CsvImportUpload"}},
                        "application/json": {"schema": {"$ref": "#/components/schemas/CsvImportJson"}},
                    },
                },
                "responses": bearer_responses(json_response("Import result", "#/components/schemas/ImportResult")),
            }
        },
        "/api/contacts/status": {
            "get": {
                "tags": ["Contacts"],
                "summary": "Get contact status",
                "security": [{"BearerAuth": []}],
                "parameters": SCOPE_QUERY_PARAMS,
                "responses": bearer_responses(json_response("Contact status", "#/components/schemas/ContactStatus")),
            }
        },
        "/api/contacts/{contact_id}/tags": {
            "put": {
                "tags": ["Tags"],
                "summary": "Replace contact tags",
                "security": [{"BearerAuth": []}],
                "parameters": [CONTACT_ID_PARAM],
                "requestBody": json_body("#/components/schemas/TagReplaceRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            }
        },
        "/api/contacts/{contact_id}/tags/{tag_slug}": {
            "post": {
                "tags": ["Tags"],
                "summary": "Add contact tag",
                "security": [{"BearerAuth": []}],
                "parameters": [CONTACT_ID_PARAM, TAG_SLUG_PARAM],
                "requestBody": json_body("#/components/schemas/ScopedMutationRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            },
            "delete": {
                "tags": ["Tags"],
                "summary": "Remove contact tag",
                "security": [{"BearerAuth": []}],
                "parameters": [CONTACT_ID_PARAM, TAG_SLUG_PARAM],
                "requestBody": json_body("#/components/schemas/ScopedMutationRequest", required=False),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            },
        },
        "/api/contacts/{contact_id}/verification": {
            "patch": {
                "tags": ["State"],
                "summary": "Update verification",
                "security": [{"BearerAuth": []}],
                "parameters": [CONTACT_ID_PARAM],
                "requestBody": json_body("#/components/schemas/VerificationRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            }
        },
        "/api/contacts/{contact_id}/validation": {
            "patch": {
                "tags": ["State"],
                "summary": "Update email validation",
                "security": [{"BearerAuth": []}],
                "parameters": [CONTACT_ID_PARAM],
                "requestBody": json_body("#/components/schemas/ValidationRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            }
        },
        "/api/contacts/{contact_id}/suppression": {
            "patch": {
                "tags": ["State"],
                "summary": "Update suppression",
                "security": [{"BearerAuth": []}],
                "parameters": [CONTACT_ID_PARAM],
                "requestBody": json_body("#/components/schemas/SuppressionRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            }
        },
        "/api/contacts/{contact_id}/history": {
            "get": {
                "tags": ["Contacts"],
                "summary": "Get contact history",
                "security": [{"BearerAuth": []}],
                "parameters": [
                    CONTACT_ID_PARAM,
                    {"name": "audience", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "client", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100}},
                    {"name": "cursor", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                ],
                "responses": bearer_responses(json_response("Contact history", "#/components/schemas/ContactHistory")),
            }
        },
        "/api/subscriptions/subscribe": {
            "post": {
                "tags": ["Subscriptions"],
                "summary": "Subscribe contact",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/SubscribeRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/Contact")),
            }
        },
        "/api/subscriptions/unsubscribe": {
            "post": {
                "tags": ["Subscriptions"],
                "summary": "Unsubscribe contact",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/UnsubscribeRequest"),
                "responses": bearer_responses(json_response("Contact state", "#/components/schemas/ContactStatus")),
            }
        },
        "/api/transactional/send": {
            "post": {
                "tags": ["Transactional"],
                "summary": "Send transactional email",
                "description": "Queues one transactional email for an active client-scoped template. Required context is validated from template catalog metadata before any contact, message, event, or queue mutation.",
                "security": [{"BearerAuth": []}],
                "requestBody": json_body("#/components/schemas/TransactionalSendRequest"),
                "responses": bearer_responses(
                    json_response("Transactional message accepted", "#/components/schemas/TransactionalSendResponse"),
                    accepted=True,
                )
                | {"409": json_response("Contact suppressed", "#/components/schemas/TransactionalSendResponse")},
            }
        },
        "/t/o/{tracking_token}.gif": {
            "get": {
                "tags": ["Public"],
                "summary": "Open tracking pixel",
                "parameters": [TRACKING_PARAM],
                "responses": {"200": {"description": "Transparent GIF"}, "404": {"description": "Transparent GIF"}},
            }
        },
        "/t/c/{tracking_token}": {
            "get": {
                "tags": ["Public"],
                "summary": "Click tracking redirect",
                "parameters": [
                    TRACKING_PARAM,
                    {"name": "u", "in": "query", "required": True, "schema": {"type": "string", "format": "uri"}},
                ],
                "responses": {
                    "302": {"description": "Redirects to destination URL"},
                    "400": {"$ref": "#/components/responses/ValidationError"},
                },
            }
        },
        "/unsubscribe/{unsubscribe_token}": {
            "get": {
                "tags": ["Public"],
                "summary": "Render unsubscribe form",
                "parameters": [UNSUBSCRIBE_PARAM],
                "responses": {"200": {"description": "HTML form"}, "404": {"description": "HTML not found"}},
            },
            "post": {
                "tags": ["Public"],
                "summary": "Apply unsubscribe",
                "parameters": [UNSUBSCRIBE_PARAM],
                "responses": {"200": {"description": "HTML confirmation"}, "400": {"description": "HTML validation error"}},
            },
        },
        "/webhooks/ses": {
            "post": {
                "tags": ["Provider"],
                "summary": "SES/SNS webhook ingress",
                "description": "Provider ingress for Amazon SES notification messages. Requests are validated as SNS messages by the webhook service.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {
                    "200": json_response("Webhook accepted"),
                    "400": {"$ref": "#/components/responses/ValidationError"},
                    "403": json_response("SNS signature rejected"),
                },
            }
        },
    },
    "components": {
        "securitySchemes": {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "Datamailer client API key",
            }
        },
        "responses": {
            "Unauthorized": json_response("Authentication failed", "#/components/schemas/ErrorResponse"),
            "Forbidden": json_response("Scope forbidden", "#/components/schemas/ErrorResponse"),
            "ValidationError": json_response("Validation error", "#/components/schemas/ErrorResponse"),
            "MethodNotAllowed": json_response("Method not allowed", "#/components/schemas/ErrorResponse"),
        },
        "schemas": {
            "ErrorResponse": {
                "type": "object",
                "required": ["error"],
                "properties": {
                    "error": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "message": {"type": "string"},
                            "fields": {"type": "object"},
                            "allowed_methods": {"type": "array", "items": {"type": "string"}},
                        },
                    }
                },
            },
            "SubscriptionStatus": {"type": "string", "enum": [choice.value for choice in SubscriptionStatus]},
            "EmailValidationStatus": {"type": "string", "enum": [choice.value for choice in EmailValidationStatus]},
            "ScopedMutationRequest": {
                "type": "object",
                "required": ["audience", "client"],
                "properties": {"audience": {"type": "string"}, "client": {"type": "string"}},
            },
            "ContactUpsertRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["email"],
                        "properties": {
                            "email": {"type": "string", "format": "email"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                            "status": {"$ref": "#/components/schemas/SubscriptionStatus"},
                            "verified": {"type": "boolean"},
                            "email_validation": {"$ref": "#/components/schemas/EmailValidationInput"},
                            "suppression": {"$ref": "#/components/schemas/SuppressionFlags"},
                        },
                    },
                ]
            },
            "SubscribeRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ContactUpsertRequest"},
                    {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}},
                ]
            },
            "UnsubscribeRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["email", "scope"],
                        "properties": {
                            "email": {"type": "string", "format": "email"},
                            "scope": {"type": "string", "enum": ["client", "audience", "global"]},
                            "reason": {"type": "string"},
                        },
                    },
                ]
            },
            "TagReplaceRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["tags"],
                        "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
                    },
                ]
            },
            "VerificationRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["verified"],
                        "properties": {
                            "verified": {"type": "boolean"},
                            "verified_at": {"type": "string", "format": "date-time"},
                        },
                    },
                ]
            },
            "ValidationRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "required": ["status"],
                        "properties": {
                            "status": {"$ref": "#/components/schemas/EmailValidationStatus"},
                            "reason": {"type": "string"},
                            "validated_at": {"type": "string", "format": "date-time"},
                        },
                    },
                ]
            },
            "EmailValidationInput": {
                "type": "object",
                "properties": {
                    "status": {"$ref": "#/components/schemas/EmailValidationStatus"},
                    "reason": {"type": "string"},
                    "validated_at": {"type": "string", "format": "date-time"},
                },
            },
            "SuppressionFlags": {
                "type": "object",
                "properties": {
                    "global_unsubscribed": {"type": "boolean"},
                    "hard_bounced": {"type": "boolean"},
                    "complained": {"type": "boolean"},
                },
            },
            "SuppressionRequest": {
                "allOf": [
                    {"$ref": "#/components/schemas/ScopedMutationRequest"},
                    {
                        "type": "object",
                        "properties": {
                            "global_unsubscribed": {"type": "boolean"},
                            "hard_bounced": {"type": "boolean"},
                            "complained": {"type": "boolean"},
                            "reason": {"type": "string"},
                        },
                    },
                ]
            },
            "BulkImportRequest": {
                "type": "object",
                "required": ["contacts"],
                "properties": {
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "dry_run": {"type": "boolean", "description": "Validate and report would-create/update actions."},
                    "idempotency_key": {"type": "string", "description": "Echoed for client-side run correlation."},
                    "contacts": {"type": "array", "items": {"$ref": "#/components/schemas/ContactUpsertRequest"}},
                },
            },
            "CsvImportUpload": {
                "type": "object",
                "required": ["file"],
                "properties": {
                    "file": {"type": "string", "format": "binary"},
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                    "idempotency_key": {"type": "string"},
                },
            },
            "CsvImportJson": {
                "type": "object",
                "required": ["csv"],
                "properties": {
                    "csv": {"type": "string"},
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "dry_run": {"type": "boolean"},
                    "idempotency_key": {"type": "string"},
                },
            },
            "ContactStatus": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": ["integer", "null"]},
                    "email": {"type": "string"},
                    "exists": {"type": "boolean"},
                    "verified": {"type": "boolean"},
                    "verified_at": {"type": ["string", "null"], "format": "date-time"},
                    "email_validation": {"$ref": "#/components/schemas/EmailValidationState"},
                    "global_unsubscribed": {"type": "boolean"},
                    "hard_bounced": {"type": "boolean"},
                    "complained": {"type": "boolean"},
                    "can_send_marketing": {"type": "boolean"},
                    "can_send_transactional": {"type": "boolean"},
                    "audience": {"$ref": "#/components/schemas/SubscriptionState"},
                    "client": {"$ref": "#/components/schemas/SubscriptionState"},
                },
            },
            "Contact": {
                "allOf": [
                    {"$ref": "#/components/schemas/ContactStatus"},
                    {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}},
                ]
            },
            "EmailValidationState": {
                "type": "object",
                "properties": {
                    "status": {"$ref": "#/components/schemas/EmailValidationStatus"},
                    "reason": {"type": "string"},
                    "validated_at": {"type": ["string", "null"], "format": "date-time"},
                },
            },
            "SubscriptionState": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "subscribed": {"type": "boolean"},
                    "status": {"type": ["string", "null"]},
                    "verified": {"type": "boolean"},
                    "verified_at": {"type": ["string", "null"], "format": "date-time"},
                    "unsubscribed_at": {"type": ["string", "null"], "format": "date-time"},
                    "unsubscribe_reason": {"type": "string"},
                },
            },
            "ContactListResponse": {
                "type": "object",
                "properties": {
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "count": {"type": "integer"},
                    "next_cursor": {"type": ["string", "null"], "description": "Pass as cursor for the next page."},
                    "contacts": {"type": "array", "items": {"$ref": "#/components/schemas/Contact"}},
                },
            },
            "ImportResult": {
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean"},
                    "idempotency_key": {"type": "string"},
                    "counts": {"type": "object", "additionalProperties": {"type": "integer"}},
                    "results": {"type": "array", "items": {"type": "object"}, "description": "Per-item/row actions."},
                    "errors": {"type": "array", "items": {"type": "object"}, "description": "Partial item/row validation errors."},
                },
            },
            "ContactHistory": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "integer"},
                    "email": {"type": "string"},
                    "audience": {"type": "string"},
                    "client": {"type": "string"},
                    "campaign_recipients": {"type": "array", "items": {"type": "object"}},
                    "transactional_messages": {"type": "array", "items": {"type": "object"}},
                    "events": {"type": "array", "items": {"type": "object"}},
                    "next_cursor": {"type": ["string", "null"]},
                },
            },
            "TransactionalSendRequest": {
                "type": "object",
                "required": ["email", "template_key"],
                "properties": {
                    "email": {"type": "string", "format": "email"},
                    "template_key": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "context": {"type": "object"},
                    "metadata": {"type": "object"},
                },
            },
            "TransactionalSendResponse": {
                "type": "object",
                "properties": {
                    "message": {"type": "object"},
                    "idempotent_replay": {"type": "boolean"},
                    "enqueued": {"type": "boolean"},
                },
            },
        },
    },
}
