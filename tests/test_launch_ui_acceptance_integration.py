from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase

from control.jwt_auth import issue_access
from control.services.api_distribution import bootstrap_api_distribution
from control.services.commercial_intelligence import bootstrap_commercial_packages
from control.services.launch_acceptance import ALLOWED, launch_acceptance_report


class LaunchUIAcceptanceIntegrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        bootstrap_api_distribution(); bootstrap_commercial_packages()
        cls.owner = get_user_model().objects.create_user(username="launch-owner", password="test", is_staff=True)

    def test_public_shell_and_documentation_have_semantic_commercial_hierarchy(self):
        landing = self.client.get("/")
        docs = self.client.get("/api/docs/")
        self.assertEqual(landing.status_code, 200); self.assertEqual(docs.status_code, 200)
        for marker in ('<main id="content">', 'id="api-business"', 'id="engine"', 'id="trust"', 'data-funnel="CTA_CLICK"', 'aria-controls="mainNav"'):
            self.assertContains(landing, marker)
        for marker in ('<main id="docs-content">', 'id="authentication"', 'id="lifecycle"', 'id="errors"', '/api/v1/products/data-cleanup/jobs'):
            self.assertContains(docs, marker)

    def test_responsive_css_fixes_layout_without_global_overflow_hiding(self):
        css = Path(finders.find("control/launch.css")).read_text(encoding="utf-8")
        console = Path(finders.find("control/console.css")).read_text(encoding="utf-8")
        self.assertNotIn("overflow-x:hidden", css.replace(" ", ""))
        self.assertNotIn("html,body{max-width:100%;overflow-x:hidden}", console.replace(" ", ""))
        for marker in ("min-width:320px", "@media(max-width:1024px)", "@media(max-width:768px)", "@media(max-width:480px)", "prefers-reduced-motion", "min-height:44px"):
            self.assertIn(marker, css)
        self.assertIn(".table-scroll", console)

    def test_navigation_is_keyboard_accessible_and_commercial_route_is_private(self):
        script = Path(finders.find("control/landing.js")).read_text(encoding="utf-8")
        for marker in ('event.key === "Escape"', 'aria-expanded', 'first.focus()', 'pointerdown'):
            self.assertIn(marker, script)
        self.assertEqual(self.client.get("/ops/commercial/").status_code, 302)
        self.client.cookies["amarktai_access"] = issue_access(self.owner)
        page = self.client.get("/ops/commercial/")
        self.assertEqual(page.status_code, 200); self.assertContains(page, 'data-section="commercial"')
        api = self.client.get("/api/ops/commercial")
        self.assertEqual(api.status_code, 200)
        payload = api.json()
        self.assertIn("api_business", payload); self.assertEqual(payload["api_business"]["mrr"], "0")
        self.assertEqual(payload["api_business"]["mrr_truth"], "NO_AUTHORITATIVE_ACTIVE_PAID_SUBSCRIPTIONS")

    def test_first_party_telemetry_never_accepts_revenue_event(self):
        accepted = self.client.post("/api/v1/telemetry/events", data={"event_type": "CTA_CLICK", "anonymous_session": "safe-anon", "source": "test"}, content_type="application/json")
        rejected = self.client.post("/api/v1/telemetry/events", data={"event_type": "SETTLED", "anonymous_session": "safe-anon"}, content_type="application/json")
        self.assertEqual(accepted.status_code, 202); self.assertEqual(rejected.status_code, 400)

    def test_launch_gate_has_exact_criteria_and_truthful_classifications(self):
        report = launch_acceptance_report(ci_proven=True)
        expected = {"CORE_EXECUTION", "CAPABILITY_ENGINEERING", "MONEY_ENGINEERING", "PUBLIC_UI", "RESPONSIVE_UI", "COMMERCIAL_API_GATEWAY", "API_PRODUCT_CATALOG", "RAPIDAPI_PACKAGE", "APIFY_COMMERCIAL_PACKAGE", "MULTI_MARKET_API_DISTRIBUTION", "CUSTOMER_ECONOMICS", "OFFER_EXPERIMENTS", "PRODUCT_PACKAGING", "CAPABILITY_EVALS", "CONVERSION_TELEMETRY", "PROFIT_EXPLAINABILITY", "AUTONOMY", "EXTERNAL_SIDE_EFFECTS"}
        self.assertEqual({row["name"] for row in report["criteria"]}, expected)
        self.assertTrue(all(row["status"] in ALLOWED for row in report["criteria"]))
        self.assertEqual(next(row for row in report["criteria"] if row["name"] == "MULTI_MARKET_API_DISTRIBUTION")["status"], "PASS")
        self.assertEqual(next(row for row in report["criteria"] if row["name"] == "AUTONOMY")["status"], "PASS")
        self.assertFalse(report["safety"]["external_mutations_performed"])