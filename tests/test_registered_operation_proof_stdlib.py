from __future__ import annotations

import os
import unittest
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from workers.registry import (
    all_operation_contracts,
    capability_coverage,
    operation_contract,
    registered_operations,
    registry_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


class RegisteredOperationProofTests(unittest.TestCase):
    def test_every_registered_operation_has_one_complete_contract_and_test_evidence(self):
        operations = registered_operations()
        contracts = all_operation_contracts()
        self.assertEqual(len(contracts), len(operations))
        self.assertEqual({row.operation for row in contracts}, set(operations))

        report = capability_coverage(repository_root=ROOT)
        self.assertEqual(report["status"], "PASS", report["operations"])
        total = len(operations)
        for key in (
            "TOTAL_REGISTERED_OPERATIONS",
            "OPERATIONS_WITH_WORKERS",
            "OPERATIONS_WITH_INPUT_CONTRACT",
            "OPERATIONS_WITH_OUTPUT_CONTRACT",
            "OPERATIONS_WITH_QA",
            "OPERATIONS_WITH_COST_POLICY",
            "OPERATIONS_WITH_FAILURE_POLICY",
            "OPERATIONS_WITH_TEST_COVERAGE",
        ):
            self.assertEqual(report["summary"][key], total, key)
        self.assertEqual(
            report["summary"]["OPERATIONS_READY"]
            + report["summary"]["OPERATIONS_BLOCKED_BY_EXTERNAL_OWNER_ACTION"],
            total,
        )

    def test_provider_operations_are_explicitly_blocked_until_live_owner_evidence_exists(self):
        for contract in all_operation_contracts():
            if contract.proof_class == "provider_contract":
                self.assertEqual(contract.owner_action_blocker, "GENX_CREDENTIAL_AND_LIVE_CATALOG_REQUIRED")
                self.assertIn("ambiguous remote state stops", contract.failure_policy)
            else:
                self.assertFalse(contract.owner_action_blocker)

    def test_registry_manifest_exposes_the_same_contracts_used_by_proof_and_dashboard(self):
        manifested = {
            row["operation"]: row
            for worker in registry_manifest()
            for row in worker["operation_contracts"]
        }
        self.assertEqual(set(manifested), set(registered_operations()))
        for operation, row in manifested.items():
            contract = operation_contract(operation)
            self.assertEqual(row["worker_class"], contract.worker_class)
            self.assertEqual(row["qa_profile"], contract.qa_profile)
            self.assertEqual(row["owner_action_blocker"], contract.owner_action_blocker)

    def test_coding_and_ci_workers_reject_wrong_operation_before_external_boundaries(self):
        report = capability_coverage(repository_root=ROOT)
        for operation in ("code_change_small", "code_change_heavy", "run_repository_tests"):
            row = next(item for item in report["operations"] if item["operation"] == operation)
            self.assertEqual(row["status"], "PASS")

    def test_image_edit_product_asset_is_covered_by_the_canonical_proof_contract(self):
        from workers.base import WorkRequest
        from workers.image_product.worker import ImageProductWorker

        contract = operation_contract("image_edit_product_asset")
        self.assertEqual(contract.provider_category, "image")
        self.assertIn("source_image_when_editing", contract.model_parameter_requirements)
        self.assertEqual(contract.external_side_effect, "paid_provider_call")
        result = ImageProductWorker().execute(WorkRequest(
            job_id="not-used-before-validation",
            workspace=ROOT / ".registry-proof-never-written",
            inputs={"operation": "image_edit_product_asset"},
        ))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "prompt and original rights-safe confirmation are required")


if __name__ == "__main__":
    unittest.main()
