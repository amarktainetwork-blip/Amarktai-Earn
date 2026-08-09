from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase

from control.jwt_auth import issue_access
from control.models import OwnerSecurityProfile


class OwnerUIIntegrationTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(
            username="ui-owner",
            password="Strong-Owner-UI-Password-2026!",
            is_staff=True,
        )
        OwnerSecurityProfile.objects.create(user=self.owner)

    def authenticate(self):
        self.client.cookies["amarktai_access"] = issue_access(self.owner)

    def test_login_page_is_branded_step_by_step_and_recovery_remains_secondary(self):
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertContains(response, "Autonomous income.")
        self.assertContains(response, 'id="accountForm"')
        self.assertContains(response, 'id="verificationStep"')
        self.assertContains(response, "Two-factor authentication")
        self.assertContains(response, "Use a recovery code")
        self.assertIn('id="verificationStep"', html)
        self.assertIn("hidden", html[html.index('id="verificationStep"') - 80:html.index('id="verificationStep"') + 140])
        self.assertNotIn("localStorage", html)

    def test_login_javascript_preserves_existing_auth_endpoints_and_never_uses_local_storage(self):
        script_path = Path(finders.find("control/login.js"))
        script = script_path.read_text(encoding="utf-8")
        self.assertIn('fetch("/api/auth/csrf"', script)
        self.assertIn('post("/api/auth/login"', script)
        self.assertIn('post("/api/auth/totp"', script)
        self.assertIn("recovery: recoveryMode", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)

    def test_unauthenticated_owner_pages_redirect_to_login(self):
        for path in ("/", "/ops/jobs/", "/ops/agents/", "/ops/money/", "/ops/markets/", "/ops/alerts/", "/ops/system/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, "/login/")

    def test_normal_and_advanced_owner_pages_render_for_authenticated_owner(self):
        self.authenticate()
        sections = (
            "jobs", "agents", "money", "markets", "alerts", "system", "genx", "nodes",
            "storage", "performance", "logs", "security", "settings", "live-work", "earnings", "treasury",
        )
        overview = self.client.get("/")
        self.assertEqual(overview.status_code, 200)
        self.assertContains(overview, 'data-section="overview"')
        self.assertContains(overview, "System &amp; Settings")
        for section in sections:
            with self.subTest(section=section):
                response = self.client.get(f"/ops/{section}/")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'data-section="{section}"')

    def test_ui_assets_are_discoverable_and_preserve_settled_cash_language(self):
        self.assertIsNotNone(finders.find("control/app.css"))
        self.assertIsNotNone(finders.find("control/app.js"))
        self.assertIsNotNone(finders.find("control/login.css"))
        app_styles = Path(finders.find("control/app.css")).read_text(encoding="utf-8")
        app_script = Path(finders.find("control/app.js")).read_text(encoding="utf-8")
        self.assertIn("[hidden]{display:none!important}", app_styles)
        self.assertIn("Only bank or rail-confirmed, reconciled SETTLED payouts", app_script)
        self.assertIn("Contract exposure, not received cash", app_script)
        self.assertIn("Earned, but not received cash", app_script)
        self.assertNotIn("fake", app_script.lower())
