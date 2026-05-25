from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse


class HealthCheckTests(SimpleTestCase):
    def test_health_returns_ok(self):
        response = self.client.get(reverse("mailing:health"))

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class DashboardTests(TestCase):
    def test_dashboard_renders_product_shell_for_staff(self):
        operator = get_user_model().objects.create_user(
            "operator",
            "operator@example.com",
            "password",
            is_staff=True,
        )
        self.client.force_login(operator)

        response = self.client.get(reverse("mailing:dashboard"))

        assert response.status_code == 200
        page = response.content.decode()
        assert "Dashboard" in page
        assert "Operational Summary" in page
        assert "/operator/" not in page

    def test_dashboard_requires_staff(self):
        response = self.client.get(reverse("mailing:dashboard"))

        assert response.status_code == 302
        assert "/admin/login/" in response["Location"]


class ProductRouteTests(SimpleTestCase):
    def test_product_ui_routes_do_not_use_operator_prefix(self):
        route_expectations = [
            ("campaign_list", [], "/campaigns/"),
            ("campaign_create", [], "/campaigns/new/"),
            ("campaign_detail", [123], "/campaigns/123/"),
            ("campaign_edit", [123], "/campaigns/123/edit/"),
            ("campaign_queue", [123], "/campaigns/123/queue/"),
            ("audience_list", [], "/audiences/"),
            ("audience_create", [], "/audiences/new/"),
            ("audience_detail", [123], "/audiences/123/"),
            ("audience_edit", [123], "/audiences/123/edit/"),
            ("tag_create", [123], "/audiences/123/tags/new/"),
            ("tag_detail", [123], "/tags/123/"),
            ("tag_edit", [123], "/tags/123/edit/"),
            ("client_list", [], "/clients/"),
            ("client_create", [], "/clients/new/"),
            ("client_detail", [123], "/clients/123/"),
            ("client_edit", [123], "/clients/123/edit/"),
            ("client_api_key_create", [123], "/clients/123/api-keys/create/"),
            ("client_api_key_revoke", [123, 456], "/clients/123/api-keys/456/revoke/"),
            ("contact_search", [], "/contacts/"),
            ("contact_detail", ["person@example.com"], "/contacts/person@example.com/"),
            ("contact_state_update", ["person@example.com"], "/contacts/person@example.com/state/"),
            ("contact_subscription_update", ["person@example.com"], "/contacts/person@example.com/subscriptions/"),
            ("contact_tag_add", ["person@example.com"], "/contacts/person@example.com/tags/add/"),
            ("contact_tag_remove", ["person@example.com"], "/contacts/person@example.com/tags/remove/"),
            ("api_docs", [], "/api-docs/"),
            ("api_docs_json", [], "/api-docs/openapi.json"),
            ("template_catalog", [], "/templates/"),
            ("template_detail", [123], "/templates/123/"),
        ]

        for route_name, args, expected_path in route_expectations:
            assert reverse(f"mailing:{route_name}", args=args) == expected_path

    def test_legacy_operator_routes_are_removed_and_admin_route_remains(self):
        assert self.client.get("/operator/campaigns/").status_code == 404
        assert self.client.get("/operator/contacts/").status_code == 404
        assert self.client.get("/operator/api-docs/").status_code == 404
        assert reverse("admin:login") == "/admin/login/"
