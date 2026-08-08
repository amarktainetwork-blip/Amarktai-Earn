import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SubmissionContractTests(unittest.TestCase):
    def test_submission_requires_qa_pass_and_precreates_remote_state(self):
        text = (ROOT / "control/services/submission.py").read_text(encoding="utf-8")
        self.assertIn('status="QA_PASSED"', text)
        self.assertIn('status="SUBMITTING"', text)
        self.assertIn('"UNKNOWN_REMOTE_STATE"', text)
        self.assertIn("acquire_job_lock", text)
        self.assertIn("Job.State.SUBMITTED", text)

    def test_webhook_view_verifies_raw_body_before_persistence(self):
        text = (ROOT / "control/webhooks.py").read_text(encoding="utf-8")
        self.assertLess(text.index("verify_signature"), text.index("WebhookEvent.objects.get_or_create"))
        self.assertIn("MAX_WEBHOOK_BYTES", text)
        self.assertIn("event_key(raw)", text)


if __name__ == "__main__":
    unittest.main()
