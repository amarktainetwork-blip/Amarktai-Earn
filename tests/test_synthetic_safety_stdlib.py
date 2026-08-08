from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workers.base import WorkRequest
from workers.qa.runtime import run_qa
from workers.registry import operation_spec
from workers.synthetic_data.worker import SyntheticDataWorker


SCHEMA = {
    "fields": {
        "text": {"type": "string", "required": True},
        "label": {"type": "string", "required": True, "enum": ["billing", "support"]},
    },
    "label_field": "label",
}


class SyntheticDataWorkerTests(unittest.TestCase):
    def test_registry_contains_first_class_synthetic_and_safety_workers(self):
        self.assertEqual(operation_spec("synthetic_dataset_generate").worker_class, "synthetic_data")
        self.assertEqual(operation_spec("ai_safety_evaluate").worker_class, "ai_safety_research")

    def test_bounded_generation_deduplicates_rejects_pii_splits_and_passes_reopen_qa(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = WorkRequest(job_id="job-1", workspace=Path(tmp), inputs={
                "operation": "synthetic_dataset_generate", "mode": "COMMISSIONED",
                "rights_confirmed": True, "provenance": {"source": "commissioned specification"},
                "schema": SCHEMA,
                "generation_plan": {"record_count": 5, "splits": {"train": 0.6, "validation": 0.2, "test": 0.2}},
                "records": [
                    {"text": "Billing question one", "label": "billing"},
                    {"text": "Billing question one", "label": "billing"},
                    {"text": "Support request", "label": "support"},
                    {"text": "Contact alice@example.com", "label": "support"},
                    {"text": 42, "label": "invalid"},
                ],
                "estimated_generation_cost": "0.10", "authorized_generation_cost": "0.20",
            })
            result = SyntheticDataWorker().execute(request)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.evidence["accepted_records"], 2)
            self.assertEqual(result.evidence["duplicate_records"], 1)
            self.assertEqual(result.evidence["pii_rejected"], 1)
            self.assertEqual(result.evidence["invalid_records"], 1)
            reopened = [json.loads(line) for line in result.artifacts[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(reopened), 2)
            self.assertTrue(all("_split" in row for row in reopened))
            qa = run_qa("synthetic_dataset", result.artifacts[0], result.evidence)
            self.assertTrue(qa.passed, qa.checks)

    def test_inventory_and_generation_cost_are_fail_closed(self):
        base = {
            "operation": "synthetic_dataset_generate", "mode": "INVENTORY",
            "rights_confirmed": True, "provenance": {"source": "owned schema"}, "schema": SCHEMA,
            "generation_plan": {"record_count": 1, "generators": {
                "text": {"type": "template", "template": "Example {index}"},
                "label": {"type": "choice", "values": ["billing"]},
            }},
            "inventory_demand_evidence": {"commission": "expected"}, "inventory_budget_authorized": True,
            "estimated_generation_cost": "1", "authorized_generation_cost": "0.50",
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SYNTHETIC_SPECULATIVE_INVENTORY_ENABLED": "0"}, clear=False):
            result = SyntheticDataWorker().execute(WorkRequest(job_id="job", workspace=Path(tmp), inputs=base))
            self.assertFalse(result.ok)
            self.assertIn("INVENTORY", result.error)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"SYNTHETIC_SPECULATIVE_INVENTORY_ENABLED": "1"}, clear=False):
            result = SyntheticDataWorker().execute(WorkRequest(job_id="job", workspace=Path(tmp), inputs=base))
            self.assertFalse(result.ok)
            self.assertIn("BUDGET", result.error)


if __name__ == "__main__":
    unittest.main()
