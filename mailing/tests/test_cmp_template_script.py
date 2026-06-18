import importlib.util
from pathlib import Path


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
