import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkPlanContractTests(unittest.TestCase):
    def test_planner_is_narrow_and_blocks_ambiguity(self):
        text = (ROOT / "planning/services.py").read_text(encoding="utf-8")
        self.assertIn('"json_to_csv"', text)
        self.assertIn('"csv_normalize"', text)
        self.assertIn('TRANSFORMATION_NOT_UNAMBIGUOUS', text)
        self.assertIn('INPUT_ASSET_NOT_STAGED', text)
        self.assertIn('MULTIPLE_INPUT_ASSETS_AMBIGUOUS', text)

    def test_worker_route_uses_operation_not_market_category(self):
        text = (ROOT / "control/services/execution.py").read_text(encoding="utf-8")
        self.assertIn("SAFE_STRUCTURED_OPERATIONS", text)
        self.assertNotIn("SAFE_STRUCTURED_TASKS", text)
        self.assertIn("allow_repair", text)

    def test_repair_is_bounded_and_submission_requires_qa(self):
        planning = (ROOT / "planning/services.py").read_text(encoding="utf-8")
        submission = (ROOT / "control/services/submission.py").read_text(encoding="utf-8")
        self.assertIn("max_repair_attempts", planning)
        self.assertIn("MAX_REPAIR_ATTEMPTS_REACHED", planning)
        self.assertIn("QA_PASSED", planning)
        self.assertIn('status="QA_PASSED"', submission)
        self.assertIn("SUBMISSION_RECONCILIATION", planning)
        self.assertIn("INPUT_ASSET_CHANGED_REPLAN_REQUIRED", planning)
        self.assertIn("WorkPlan.Status.FAILED", planning)
        self.assertIn("reconcile_submission_plans", planning)
        self.assertIn('status__in=[WorkPlan.Status.READY, WorkPlan.Status.NEEDS_REPAIR]', planning)

    def test_agentgigs_watcher_dispatches_awarded_work_every_cycle(self):
        text = (ROOT / "control/management/commands/run_agentgigs_watcher.py").read_text(encoding="utf-8")
        self.assertIn("dispatch_awarded_jobs", text)
        self.assertIn('marketplace_slug="agentgigs"', text)


if __name__ == "__main__":
    unittest.main()
