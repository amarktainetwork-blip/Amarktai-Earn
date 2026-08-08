import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AgentGigsRuntimeContractTests(unittest.TestCase):
    def test_application_id_and_duplicate_application_safety(self):
        text = (ROOT / "control/services/acquisition_runtime.py").read_text(encoding="utf-8")
        self.assertIn('response.get("applicationId")', text)
        self.assertIn('Application.objects.filter(job=job).exists()', text)

    def test_webhook_processing_does_not_wrap_remote_calls_in_one_atomic_block(self):
        text = (ROOT / "control/services/agentgigs.py").read_text(encoding="utf-8")
        start = text.index("def process_webhook_event")
        body = text[start:text.index("def process_pending_webhooks", start)]
        self.assertIn("_claim_webhook_event", body)
        self.assertIn("_finish_webhook_event", body)
        self.assertNotIn("with transaction.atomic():\n        event = WebhookEvent.objects.select_for_update()", body)
        self.assertIn("UNKNOWN", (ROOT / "control/services/submission.py").read_text(encoding="utf-8"))

    def test_scoring_is_prior_based_and_auto_apply_requires_two_switches(self):
        text = (ROOT / "control/services/agentgigs.py").read_text(encoding="utf-8")
        self.assertIn("PRIOR_BASED_SCORE", text)
        self.assertIn('AUTONOMOUS_MODE', text)
        self.assertIn('AGENTGIGS_AUTO_APPLY_ENABLED', text)
        self.assertIn('WORKER_CAPABILITY_NOT_LIVE', text)
        self.assertIn('recommended_offer', text)

    def test_watcher_prioritizes_webhooks_before_new_discovery(self):
        text = (ROOT / "control/services/agentgigs.py").read_text(encoding="utf-8")
        start = text.index("def run_cycle")
        body = text[start:]
        self.assertLess(body.index("process_pending_webhooks"), body.index("sync_market"))
        self.assertLess(body.index("sync_market"), body.index("attempt_profitable_applications"))

    def test_late_payout_reconciliation_can_attach_to_accepted_job(self):
        finance = (ROOT / "control/services/finance.py").read_text(encoding="utf-8")
        agentgigs = (ROOT / "control/services/agentgigs.py").read_text(encoding="utf-8")
        self.assertIn("Job.State.SUBMITTED, Job.State.ACCEPTED", finance)
        self.assertIn("job.state in {Job.State.SUBMITTED, Job.State.ACCEPTED}", agentgigs)

    def test_third_migration_persists_webhook_retry_and_revision_idempotency(self):
        text = (ROOT / "control/migrations/0003_market_webhooks.py").read_text(encoding="utf-8")
        for value in ("attempt_count", "last_attempt_at", "source_event_key", "recommended_offer"):
            self.assertIn(value, text)


if __name__ == "__main__":
    unittest.main()
