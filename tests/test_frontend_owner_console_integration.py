from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase

from control.jwt_auth import issue_access
from control.models import Job, Marketplace
from control.ops import agents_snapshot, live_work_snapshot, overview_snapshot
from workers.registry import all_operation_contracts


class FrontendOwnerConsoleCompletionTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(
            username="frontend-owner",
            password="Frontend-Owner-Password-2026!",
            is_staff=True,
        )
        self.market = Marketplace.objects.create(slug="frontend-market", display_name="Frontend Market")
        self.job = Job.objects.create(
            marketplace=self.market,
            external_id="frontend-job",
            title="Frontend truth binding job",
            task_class="documents",
            reward="75.00",
            state=Job.State.DISCOVERED,
        )

    def authenticate(self):
        self.client.cookies["amarktai_access"] = issue_access(self.owner)

    def test_public_site_has_metadata_mobile_navigation_and_product_explanation(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for marker in (
            'name="robots"', 'property="og:title"', 'id="navToggle"', 'id="mainNav"',
            "Profit Brain", "GenX execution", "Approval controls", "Settlement truth",
        ):
            self.assertContains(response, marker)
        self.assertIsNotNone(finders.find("control/landing.js"))

    def test_login_has_loading_failure_lockout_and_session_expiry_presentation(self):
        response = self.client.get("/login/?reason=session-expired")
        self.assertContains(response, 'id="sessionNotice"')
        self.assertContains(response, 'aria-live="assertive"')
        script = Path(finders.find("control/login.js")).read_text(encoding="utf-8")
        for marker in ("Retry-After", "Access temporarily locked", "Verification locked", "session-expired", "classList.toggle(\"loading\""):
            self.assertIn(marker, script)

    def test_new_owner_surfaces_resolve_and_remain_private(self):
        paths = ("capabilities", "services", "genx", "audit")
        for section in paths:
            response = self.client.get(f"/ops/{section}/")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, "/login/")
        self.authenticate()
        for section in paths:
            response = self.client.get(f"/ops/{section}/")
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, f'data-section="{section}"')

    def test_registry_capability_payload_is_dynamic_complete_and_truthful(self):
        payload = agents_snapshot()
        contracts = all_operation_contracts()
        self.assertEqual(payload["meta"]["TOTAL_REGISTERED_OPERATIONS"], len(contracts))
        self.assertEqual(len(payload["operations"]), len(contracts))
        self.assertEqual(payload["meta"]["OPERATIONS_BLOCKED_BY_EXTERNAL_OWNER_ACTION"], 12)
        self.assertEqual(
            payload["meta"]["OPERATIONS_READY"] + payload["meta"]["OPERATIONS_BLOCKED_BY_EXTERNAL_OWNER_ACTION"],
            len(contracts),
        )
        required = {"operation", "worker_class", "input_contract", "runtime_capability", "qa_profile", "cost_policy", "failure_policy", "owner_action_blocker", "status"}
        self.assertTrue(all(required <= row.keys() for row in payload["operations"]))
        for row in payload["operations"]:
            if row["owner_action_blocker"]:
                self.assertEqual(row["status"], "EXTERNAL_PROOF_REQUIRED")
                self.assertEqual(row["owner_action_blocker"], "GENX_CREDENTIAL_AND_LIVE_CATALOG_REQUIRED")

    def test_overview_and_job_payloads_bind_operating_and_detail_truth(self):
        overview = overview_snapshot()
        labels = {row["label"] for row in overview["cards"]}
        self.assertTrue({"GROSS SETTLED 30D", "SETTLED FEES 30D", "FAILED JOBS", "OWNER ACTIONS", "CRITICAL ALERTS"} <= labels)
        self.assertIn(overview["meta"]["production_state"], {"OFF", "SHADOW", "READY", "BLOCKED"})
        self.assertIn(overview["meta"]["system_health"], {"HEALTHY", "ATTENTION_REQUIRED", "NO_SNAPSHOT"})
        row = live_work_snapshot()["rows"][0]
        required = {"fee", "expected_profit", "actual_profit", "actual_profit_truth", "execution_history", "genx_calls", "artifact_rows", "payout", "timeline", "qualification_decision"}
        self.assertTrue(required <= row.keys())
        self.assertIsNone(row["actual_profit"])
        self.assertIn("unavailable until settlement", row["actual_profit_truth"])

    def test_frontend_uses_one_status_system_and_all_required_renderers(self):
        script = Path(finders.find("control/app.js")).read_text(encoding="utf-8")
        for marker in (
            "const goodStates", "const warningStates", "function stateBadge", "function updateGlobalStatus",
            "function renderCapabilities", "function renderMoney", "function renderGenX", "function renderServices",
            "function renderAudit", "function alertGuidance", "WORKFLOW STATE", "Audit timeline",
        ):
            self.assertIn(marker, script)
        self.assertNotIn("TOTAL_REGISTERED_OPERATIONS = 42", script)
        self.assertNotIn("model: \"", script)

    def test_responsive_structural_contract_prevents_page_overflow(self):
        console = Path(finders.find("control/console.css")).read_text(encoding="utf-8")
        landing = Path(finders.find("control/landing-shell.css")).read_text(encoding="utf-8")
        for marker in ("html,body{max-width:100%;overflow-x:hidden}", ".table-scroll{max-width:100%", "@media(max-width:1080px)", "@media(max-width:768px)", "@media(max-width:480px)"):
            self.assertIn(marker, console)
        self.assertIn(".main-nav.open{display:grid}", landing)
        self.assertIn("@media (max-width:720px)", landing)

    def test_console_source_never_embeds_secret_values_or_invented_finance(self):
        self.authenticate()
        for section in ("overview", "agents", "earnings", "treasury", "genx", "accounts", "factory", "logs"):
            response = self.client.get(f"/api/ops/{section}")
            self.assertEqual(response.status_code, 200)
            rendered = response.content.decode().lower()
            self.assertNotIn("encrypted_value", rendered)
            self.assertNotIn("frontend-owner-password", rendered)
        script = Path(finders.find("control/app.js")).read_text(encoding="utf-8")
        self.assertIn("No profitability aggregates yet", script)
        self.assertIn("no invented chart points", script)

