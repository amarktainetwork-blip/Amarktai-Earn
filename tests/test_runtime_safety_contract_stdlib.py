import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RuntimeSafetyContractTests(unittest.TestCase):
    def test_execution_restricts_inputs_and_artifacts_to_persistent_roots(self):
        text = (ROOT / "control/services/execution.py").read_text(encoding="utf-8")
        self.assertIn("AMARKTAI_UPLOAD_ROOT", text)
        self.assertIn("input source is outside approved upload/job storage", text)
        self.assertIn("worker artifact escaped execution workspace", text)
        self.assertIn("renew_job_lock", text)

    def test_acquisition_marks_uncertain_remote_calls_instead_of_replaying_blindly(self):
        text = (ROOT / "control/services/acquisition_runtime.py").read_text(encoding="utf-8")
        self.assertIn('status="SUBMITTING"', text)
        self.assertIn("UNKNOWN_REMOTE_STATE", text)
        self.assertIn('"UNKNOWN_REMOTE_STATE"', text)
        self.assertIn("_ensure_no_active_attempt", text)

    def test_genx_request_key_does_not_auto_replay(self):
        text = (ROOT / "gateways/genx/service.py").read_text(encoding="utf-8")
        self.assertIn("if not created", text)
        self.assertIn("Never replay a request key automatically", text)
        self.assertIn('status="SUBMITTING"', text)
        self.assertIn("UNKNOWN_REMOTE_STATE", text)
        self.assertIn("reconcile_pending", text)


if __name__ == "__main__":
    unittest.main()
