import importlib.util
from pathlib import Path

from django.template import Context, Template


def load_script_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "upsert_cmp_templates.py"
    spec = importlib.util.spec_from_file_location("upsert_cmp_templates", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cmp_template_script_covers_cmp_template_keys():
    module = load_script_module()

    assert set(module.TEMPLATES) == {
        "homework-submission-confirmation",
        "project-submission-confirmation",
        "homework-score-notification",
        "project-score-notification",
        "certificate-availability-notification",
        "deadline-reminder",
    }
    for payload in module.TEMPLATES.values():
        assert payload["subject"]
        assert payload["html_body"]
        assert payload["text_body"]
        assert payload["required_context"]
        assert payload["example_context"]
        assert payload["is_active"] is True


def test_cmp_templates_render_examples_with_action_links_and_preference_footers():
    module = load_script_module()

    for payload in module.TEMPLATES.values():
        context = Context(payload["example_context"])
        html = Template(payload["html_body"]).render(context)
        text = Template(payload["text_body"]).render(context)

        assert "{{" not in html
        assert "{{" not in text

    homework_score = module.TEMPLATES["homework-score-notification"]
    homework_html = Template(homework_score["html_body"]).render(Context(homework_score["example_context"]))
    assert "Review your homework score" in homework_html
    assert "Check the course leaderboard" in homework_html
    assert "accounts/settings" in homework_html

    project_score = module.TEMPLATES["project-score-notification"]
    project_html = Template(project_score["html_body"]).render(Context(project_score["example_context"]))
    assert "Review your project result" in project_html
    assert "GitHub repository" in project_html
    assert "accounts/settings" in project_html
