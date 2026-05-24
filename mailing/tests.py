from django.test import SimpleTestCase
from django.urls import reverse


class HealthCheckTests(SimpleTestCase):
    def test_health_returns_ok(self):
        response = self.client.get(reverse("mailing:health"))

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class DashboardTests(SimpleTestCase):
    def test_dashboard_renders_operator_shell(self):
        response = self.client.get(reverse("mailing:dashboard"))

        assert response.status_code == 200
        assert "Datamailer" in response.content.decode()
