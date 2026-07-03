from dataclasses import dataclass

from django.db.models import Count, Q

from mailing.models import EmailTemplate, TransactionalMessage, TransactionalMessageStatus
from mailing.services.api import ApiValidationError
from mailing.services.operator_ui import Badge, delivery_tone
from mailing.services.transactional_rendering import render_template_string


@dataclass(frozen=True)
class ContextRequirement:
    name: str
    description: str = ""


@dataclass(frozen=True)
class TemplateCatalogRow:
    template: EmailTemplate
    requirements: tuple[ContextRequirement, ...]
    hidden_requirement_count: int


@dataclass(frozen=True)
class RecentMessageRow:
    message: object
    badge: Badge


def normalize_required_context(value):
    requirements = []
    for item in value or []:
        if isinstance(item, str):
            name = item.strip()
            description = ""
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            description = str(item.get("description") or "").strip()
        else:
            continue
        if name:
            requirements.append(ContextRequirement(name=name, description=description))
    return tuple(requirements)


def required_context_names(template):
    return tuple(requirement.name for requirement in normalize_required_context(template.required_context))


def validate_template_context(template, context):
    errors = {}
    for name in required_context_names(template):
        if name not in context or context[name] in (None, ""):
            errors[f"context.{name}"] = "required"
    if errors:
        raise ApiValidationError(errors)


def transactional_template_queryset():
    return (
        EmailTemplate.objects.filter(is_transactional=True)
        .select_related("client", "client__organization")
        .annotate(
            queued_count=Count(
                "transactional_messages",
                filter=Q(transactional_messages__status=TransactionalMessageStatus.QUEUED),
                distinct=True,
            ),
            sent_count=Count(
                "transactional_messages",
                filter=Q(transactional_messages__status=TransactionalMessageStatus.SENT),
                distinct=True,
            ),
            skipped_count=Count(
                "transactional_messages",
                filter=Q(transactional_messages__status=TransactionalMessageStatus.SKIPPED),
                distinct=True,
            ),
            failed_count=Count(
                "transactional_messages",
                filter=Q(transactional_messages__status=TransactionalMessageStatus.FAILED),
                distinct=True,
            ),
            total_message_count=Count("transactional_messages", distinct=True),
        )
        .order_by("client__organization__slug", "client__slug", "key")
    )


def filter_transactional_templates(queryset, client_id):
    if str(client_id).isdigit():
        return queryset.filter(client_id=client_id)
    return queryset


def template_catalog_rows(templates, *, visible_requirements=3):
    rows = []
    for template in templates:
        requirements = normalize_required_context(template.required_context)
        rows.append(
            TemplateCatalogRow(
                template=template,
                requirements=requirements[:visible_requirements],
                hidden_requirement_count=max(len(requirements) - visible_requirements, 0),
            )
        )
    return rows


def transactional_queue_queryset(client):
    return (
        TransactionalMessage.objects.filter(client=client, status=TransactionalMessageStatus.QUEUED)
        .select_related("contact", "template")
        .order_by("created_at", "id")
    )


def recent_message_rows(messages):
    return tuple(
        RecentMessageRow(
            message=message,
            badge=Badge(message.get_status_display(), delivery_tone(message.status)),
        )
        for message in messages
    )


def catalog_context(template):
    requirements = normalize_required_context(template.required_context)
    example_context = template.example_context if isinstance(template.example_context, dict) else {}
    return {
        "template": template,
        "requirements": requirements,
        "example_context": example_context,
        "preview": render_preview(template, example_context),
    }


def render_preview(template, example_context, *, max_chars=1200):
    preview = {
        "subject": render_template_string(template.subject, example_context),
        "text_body": render_template_string(template.text_body, example_context),
        "html_body": render_template_string(template.html_body, example_context),
    }
    return {key: truncate(value, max_chars) for key, value in preview.items() if value}


def truncate(value, max_chars):
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars].rstrip()}..."
