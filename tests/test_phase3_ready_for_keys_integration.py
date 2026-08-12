from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from control.models import Artifact, AuditEvent, Execution, Job, Marketplace, Worker
from control.services.phase3_acceptance import PASS, READY_CREDENTIAL, READY_OWNER, READY_PRODUCTION, phase3_acceptance_report
from control.services.self_improvement import SelfImprovementGateError, create_self_improvement_review_manifest


PRELIVE_NAMES = (
    "GenX live catalogue synchronization",
    "GenX actual billing and monetary cost truth",
    "Dimensionally valid task-specific routing",
    "All paid paths use economic routing",
    "Product Factory lanes",
    "Budgets, inventory and stop-loss",
    "Capability monetization matrix",
    "Publication-ready inventory",
    "Authoritative ROI learning",
    "Paystack charge is not settlement",
    "Paystack Settlement API truth",
    "External event idempotency",
    "20-account canonical catalogue",
    "Connection test or manual boundary",
    "Priority channels",
    "Bounded credential-aware cycle",
    "Write-only encrypted credentials",
    "No bank or wallet private-key schema",
    "Revocation fail-closed",
)

V1_IDS = (
    "multifile_composite", "expanded_worker_qa", "growth_governor", "uncapped_profit_governor",
    "utilization_economics", "adaptive_economic_learning", "seller_pricing_profit_floor",
    "inbound_order_uses_canonical_job_lifecycle", "global_portfolio_ranking", "acquisition_gates",
    "qa_repair", "lifecycle_logic", "only_settled_is_cash", "money_truth",
)

FAMILIES = (
    "web_browser", "documents", "code_software", "audio", "video", "media_generation",
)


class Phase3CompositionTests(SimpleTestCase):
    def _phase2(self):
        rows = []
        for family in FAMILIES:
            rows.append({"kind": "operation", "name": f"fixture-{family}", "family": family, "status": PASS})
        return {
            "status": PASS,
            "summary": {"FAIL": 0, "PARTIAL": 0, "UNKNOWN": 0},
            "rows": rows,
        }

    def _prelive(self):
        return {
            "result": PASS,
            "code_blocker_count": 0,
            "criteria": [
                {"name": name, "status": PASS, "evidence": f"fixture proof for {name}"}
                for name in PRELIVE_NAMES
            ],
        }

    def _v1(self):
        criteria = [
            {"id": identifier, "title": identifier, "status": PASS, "operator_action": ""}
            for identifier in V1_IDS
        ]
        criteria.append({
            "id": "public_https",
            "title": "Public HTTPS",
            "status": "EXTERNAL_PROOF_REQUIRED",
            "operator_action": "Verify target production TLS.",
        })
        return {
            "overall_status": "EXTERNAL_PROOF_REQUIRED",
            "counts": {"PASS": len(V1_IDS), "FAIL": 0, "BLOCKED": 0, "EXTERNAL_PROOF_REQUIRED": 1},
            "criteria": criteria,
            "ci_proven_context": True,
        }

    def test_ready_for_keys_maps_external_activation_without_converting_it_to_failure(self):
        connections = [
            {"slug": "genx", "name": "GenX", "category": "PROVIDER", "status": READY_CREDENTIAL},
            {"slug": "contra", "name": "Contra", "category": "PROJECT_MARKET", "status": READY_OWNER},
        ]
        with patch("control.services.phase3_acceptance.phase2_acceptance_report", return_value=self._phase2()), patch(
            "control.services.phase3_acceptance.prelive_acceptance_report", return_value=self._prelive()
        ), patch(
            "control.services.phase3_acceptance.build_acceptance_report", return_value=self._v1()
        ), patch(
            "control.services.phase3_acceptance.self_improvement_contract",
            return_value={"status": PASS, "production_self_merge": False, "production_self_deploy": False},
        ), patch(
            "control.services.phase3_acceptance._connection_rows", return_value=connections
        ), patch.dict("os.environ", {"AUTONOMOUS_MODE": "OFF"}, clear=False):
            report = phase3_acceptance_report(ci_proven=True)
        self.assertEqual(report["status"], PASS)
        self.assertTrue(report["engineering_ready_for_keys"])
        self.assertEqual(report["summary"]["FAILURES"], 0)
        self.assertEqual(report["summary"]["PARTIAL"], 0)
        self.assertEqual(report["summary"]["UNKNOWN"], 0)
        self.assertEqual(report["external_connections"][0]["status"], READY_CREDENTIAL)
        self.assertEqual(report["external_connections"][1]["status"], READY_OWNER)
        self.assertEqual(report["external_production_proofs"][0]["status"], READY_PRODUCTION)

    def test_phase3_fails_when_phase2_has_engineering_failure(self):
        phase2 = self._phase2()
        phase2["status"] = "FAIL"
        phase2["summary"]["FAIL"] = 1
        with patch("control.services.phase3_acceptance.phase2_acceptance_report", return_value=phase2), patch(
            "control.services.phase3_acceptance.prelive_acceptance_report", return_value=self._prelive()
        ), patch(
            "control.services.phase3_acceptance.build_acceptance_report", return_value=self._v1()
        ), patch(
            "control.services.phase3_acceptance.self_improvement_contract",
            return_value={"status": PASS, "production_self_merge": False, "production_self_deploy": False},
        ), patch("control.services.phase3_acceptance._connection_rows", return_value=[]), patch.dict(
            "os.environ", {"AUTONOMOUS_MODE": "OFF"}, clear=False
        ):
            report = phase3_acceptance_report(ci_proven=True)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("CAPABILITIES", [row["name"] for row in report["core"] if row["status"] == "FAIL"])


class SelfImprovementReviewBoundaryTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.market = Marketplace.objects.create(slug="self-improve-test", display_name="Self Improve Test")
        self.job = Job.objects.create(
            marketplace=self.market,
            external_id="self-improve-fixture",
            title="Improve a verified capability gap",
            task_class="Coding",
            reward="100.00",
            currency="USD",
            state=Job.State.EXECUTING,
        )
        self.code_worker = Worker.objects.create(id="self-code", worker_class="code_heavy", status="READY")
        self.test_worker = Worker.objects.create(id="self-test", worker_class="ci_testing", status="READY")
        self.code_execution = Execution.objects.create(
            job=self.job,
            worker=self.code_worker,
            attempt=1,
            status="QA_PASSED",
            workspace=str(self.root / "code"),
        )
        self.test_execution = Execution.objects.create(
            job=self.job,
            worker=self.test_worker,
            attempt=2,
            status="QA_PASSED",
            workspace=str(self.root / "test"),
        )
        Path(self.code_execution.workspace).mkdir(parents=True)
        Path(self.test_execution.workspace).mkdir(parents=True)
        patch_path = Path(self.code_execution.workspace) / "change.patch"
        test_path = Path(self.test_execution.workspace) / "test-report.txt"
        patch_path.write_text("fixture patch\n", encoding="utf-8")
        test_path.write_text("all independent tests passed\n", encoding="utf-8")
        Artifact.objects.create(
            job=self.job, execution=self.code_execution, path=str(patch_path), size_bytes=patch_path.stat().st_size,
            sha256="a" * 64, mime_type="text/plain", accepted=True,
        )
        Artifact.objects.create(
            job=self.job, execution=self.test_execution, path=str(test_path), size_bytes=test_path.stat().st_size,
            sha256="b" * 64, mime_type="text/plain", accepted=True,
        )

    def test_review_manifest_is_idempotent_and_never_allows_merge_or_deploy(self):
        kwargs = {
            "job_id": self.job.id,
            "branch_name": "self-improve/fixture-gap",
            "gap_summary": "A verified capability gap requires a bounded isolated code correction with independent tests.",
            "source_refs": ["internal:test-evidence", "https://example.com/primary-source"],
            "oss_candidates": [{
                "name": "Fixture OSS",
                "url": "https://github.com/example/fixture",
                "license": "MIT",
                "approved": True,
            }],
            "code_execution_id": self.code_execution.id,
            "test_execution_id": self.test_execution.id,
        }
        first = create_self_improvement_review_manifest(**kwargs)
        second = create_self_improvement_review_manifest(**kwargs)
        self.assertFalse(first["manifest"]["merge_allowed"])
        self.assertFalse(first["manifest"]["deploy_allowed"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertTrue(second["already_recorded"])
        self.assertEqual(AuditEvent.objects.filter(event_type="self_improvement.review_ready").count(), 1)
        audit = AuditEvent.objects.get(event_type="self_improvement.review_ready")
        self.assertFalse(audit.metadata["github_mutation_performed"])
        self.assertFalse(audit.metadata["merge_performed"])
        self.assertFalse(audit.metadata["deploy_performed"])

    def test_direct_main_branch_is_rejected(self):
        with self.assertRaisesRegex(SelfImprovementGateError, "isolated self-improve"):
            create_self_improvement_review_manifest(
                job_id=self.job.id,
                branch_name="main",
                gap_summary="A verified capability gap requires a bounded isolated code correction with independent tests.",
                source_refs=["internal:test-evidence"],
                oss_candidates=[],
                code_execution_id=self.code_execution.id,
                test_execution_id=self.test_execution.id,
            )
