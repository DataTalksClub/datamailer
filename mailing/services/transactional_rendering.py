from django.template import Context, Template


def render_template_string(value, context):
    if not value:
        return ""
    return Template(value).render(Context(context, autoescape=False))
