from dataclasses import dataclass

from django.db.models import Count, Q

from mailing.models import EmailTemplate, TransactionalMessageStatus
from mailing.services.api import ApiValidationError
from mailing.services.transactional_rendering import render_template_string


@dataclass(frozen=True)
class ContextRequirement:
    name: str
    description: str = ""


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
