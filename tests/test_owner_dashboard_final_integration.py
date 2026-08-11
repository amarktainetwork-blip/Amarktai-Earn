import os
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase

from control.jwt_auth import issue_access
from control.models import (
    Artifact,
    Execution,
    Job,
    MarketIntegrationProfile,
    Marketplace,
    QAResult,
    Worker,
)
from control.ops import snapshot
from control.services.integration_accounts import (
    DEFINITIONS,
    integration_account_row,
    integration_accounts_snapshot,
    store_credentials,
)
from control.services.product_factory import factory_snapshot
from planning.acceptance import compile_acceptance_contract, evaluate_execution_acceptance
from planning.models import WorkPlan


class FinalOwnerDashboardAcceptanceTests(TestCase):
    """Acceptance tests A–T from the final owner-control-centre contract."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="final-owner", password="test-password", is_staff=True)
        self.market = Marketplace.objects.create(slug="dashboard-final", display_name="Dashboard Final")
        self.job = Job.objects.create(
            marketplace=self.market,
            external_id="dashboard-final-job",
            title="Final dashboard proof job",
            task_class="documents",
            reward="10.00",
            state=Job.State.EXECUTING,
            normalized_payload={"description": "Produce a verified artifact."},
        )
        self.plan = WorkPlan.objects.create(job=self.job, worker_class="documents", operation="document_rewrite", input_spec={}, status=WorkPlan.Status.EXECUTING)
        self.worker = Worker.objects.create(id="final-dashboard-worker", worker_class="documents", status="EXECUTING", current_job=self.job)

    def authenticate(self):
        self.client.cookies["amarktai_access"] = issue_access(self.user)

    def evaluated_job(self, *, semantic=False):
        if semantic:
            self.job.normalized_payload = {"acceptance_criteria": ["Meaning is preserved."]}
            self.job.save(update_fields=["normalized_payload", "updated_at"])
        execution = Execution.objects.create(job=self.job, worker=self.worker, attempt=1, status="QA_PASSED", result={})
        Artifact.objects.create(job=self.job, execution=execution, path="/tmp/final.txt", sha256="a" * 64, size_bytes=1)
        QAResult.objects.create(job=self.job, execution=execution, check_type="deterministic", passed=True, score=1)
        return evaluate_execution_acceptance(execution.id)

    def test_a_every_primary_owner_route_resolves(self):
        self.authenticate()
        for path in ("/ops/overview/", "/ops/jobs/", "/ops/markets/", "/ops/agents/", "/ops/money/", "/ops/treasury/", "/ops/alerts/", "/ops/settings/"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_b_unauthenticated_requests_remain_protected(self):
        for path in ("/ops/overview/", "/ops/jobs/", "/ops/markets/", "/ops/agents/", "/ops/money/", "/ops/treasury/", "/ops/alerts/", "/ops/settings/"):
            self.assertEqual(self.client.get(path).url, "/login/", path)

    def test_c_every_primary_navigation_link_maps_to_a_real_route(self):
        self.authenticate(); html = self.client.get("/ops/overview/").content.decode()
        for path in ("/ops/overview/", "/ops/jobs/", "/ops/markets/", "/ops/agents/", "/ops/money/", "/ops/treasury/", "/ops/alerts/", "/ops/settings/"):
            self.assertIn(f'href="{path}"', html)

    def test_d_no_obsolete_primary_navigation_routes_remain(self):
        self.authenticate(); html = self.client.get("/ops/overview/").content.decode()
        for path in ("/ops/system/", "/ops/genx/", "/ops/nodes/", "/ops/storage/", "/ops/performance/", "/ops/logs/", "/ops/security/", "/ops/banking/"):
            self.assertNotIn(f'href="{path}"', html)

    def test_e_account_registry_renders_all_canonical_accounts(self):
        payload = integration_accounts_snapshot()
        self.assertEqual(len(payload["rows"]), len(DEFINITIONS))
        self.assertEqual(len(payload["rows"]), 20)

    def test_f_credential_state_is_truthful(self):
        paystack = next(row for row in integration_accounts_snapshot()["rows"] if row["slug"] == "paystack")
        self.assertEqual(paystack["credential_state"], "NOT_CONFIGURED")
        self.assertFalse(paystack["connected"])

    def test_g_no_saved_secret_value_is_rendered(self):
        key = Fernet.generate_key().decode()
        with self.settings(FIELD_ENCRYPTION_ACTIVE_KID="v1", FIELD_ENCRYPTION_KEYS={"v1": key}):
            store_credentials("paystack", {"secret_key": "sk_secret_never_render"}, actor="owner")
            payload = integration_accounts_snapshot()
        self.assertNotIn("sk_secret_never_render", str(payload))
        self.assertNotIn("encrypted_value", str(payload))

    def test_h_work_ready_and_cash_ready_come_from_backend_truth(self):
        definition = next(row for row in DEFINITIONS if row.slug == "paystack")
        profile = MarketIntegrationProfile.objects.create(marketplace=Marketplace.objects.create(slug="paystack", display_name="Paystack"), work_capability_state="VERIFIED", payout_receipt_proof_state="VERIFIED")
        row = integration_account_row(definition)
        self.assertTrue(row["work_ready"]); self.assertTrue(row["cash_ready"])
        profile.work_capability_state = "UNVERIFIED"; profile.save(update_fields=["work_capability_state", "updated_at"])
        self.assertFalse(integration_account_row(definition)["work_ready"])

    def test_i_earnings_does_not_mix_expected_and_settled_revenue(self):
        script = Path(finders.find("control/app.js")).read_text(encoding="utf-8")
        self.assertIn("Only bank or rail-confirmed, reconciled SETTLED payouts", script)
        self.assertIn("Contract exposure, not received cash", script)

    def test_j_treasury_preserves_payment_payout_settlement_distinctions(self):
        script = Path(finders.find("control/app.js")).read_text(encoding="utf-8")
        self.assertIn("Payment routes, payout state, settlement, reconciliation, and owner receipt remain separate", script)

    def test_k_acceptance_contract_state_renders(self):
        contract = compile_acceptance_contract(self.job, self.plan)
        row = snapshot("live-work")["rows"][0]
        self.assertEqual(row["acceptance_contract"], contract.status)
        self.assertEqual(row["acceptance_contract_version"], 1)

    def test_l_semantic_judge_state_renders(self):
        evaluation = self.evaluated_job()
        row = snapshot("live-work")["rows"][0]
        self.assertEqual(row["semantic_acceptance"], evaluation.semantic_state)

    def test_m_critical_acceptance_failure_blocks_submission_readiness(self):
        evaluation = self.evaluated_job(semantic=True)
        row = snapshot("live-work")["rows"][0]
        self.assertFalse(evaluation.submission_ready)
        self.assertFalse(row["submission_ready"])
        self.assertTrue(row["acceptance_failures"])

    def test_n_product_factory_disabled_state_is_truthful(self):
        with patch.dict(os.environ, {"PRODUCT_FACTORY_ENABLED": "0"}, clear=False):
            self.assertFalse(factory_snapshot()["policy"]["enabled"])

    def test_o_autonomy_off_renders_truthfully(self):
        with patch.dict(os.environ, {"AUTONOMOUS_MODE": "OFF"}, clear=False):
            self.assertEqual(snapshot("overview")["meta"]["autonomous_mode"], "OFF")

    def test_p_owner_action_blockers_render(self):
        row = integration_accounts_snapshot()["rows"][0]
        self.assertTrue(row["owner_action_required"])
        self.assertIn(row["owner_action_required"], str(integration_accounts_snapshot()))

    def test_q_drawers_have_bounded_viewport_sizing_and_overflow(self):
        styles = Path(finders.find("control/app.css")).read_text(encoding="utf-8")
        self.assertIn(".drawer-panel{max-height:100dvh;overflow-y:auto", styles)
        self.assertIn("overscroll-behavior:contain", styles)

    def test_r_main_dashboard_text_tokens_resolve_to_white(self):
        styles = Path(finders.find("control/app.css")).read_text(encoding="utf-8")
        self.assertIn(".app-shell{--text:#fff;--muted:#fff;--muted-2:#fff}", styles)
        self.assertIn("color:#fff!important", styles)

    def test_s_no_visible_placeholder_action_remains(self):
        script = Path(finders.find("control/app.js")).read_text(encoding="utf-8").lower()
        self.assertNotIn("coming soon", script); self.assertNotIn('href="#"', script); self.assertNotIn("not implemented", script)

    def test_t_frontend_uses_canonical_market_readiness_without_recalculation(self):
        script = Path(finders.find("control/app.js")).read_text(encoding="utf-8")
        self.assertIn("return row.ready_for_verified_work === true", script)
        self.assertNotIn("row.enabled && row.status", script)
