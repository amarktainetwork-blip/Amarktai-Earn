import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class V1AcceptanceContracts(unittest.TestCase):
    def test_acceptance_command_has_only_honest_machine_statuses(self):
        service = (ROOT / "control/services/v1_acceptance.py").read_text(encoding="utf-8")
        command = (ROOT / "control/management/commands/v1_acceptance.py").read_text(encoding="utf-8")
        for status in ("PASS", "FAIL", "BLOCKED", "EXTERNAL_PROOF_REQUIRED"):
            self.assertIn(status, service)
        self.assertIn("--ci-proven", command)
        self.assertIn("--fail-on", command)

    def test_external_production_criteria_can_never_use_the_ci_gate(self):
        service = (ROOT / "control/services/v1_acceptance.py").read_text(encoding="utf-8")
        for criterion in ("public_https", "actual_reboot", "live_genx", "live_market_account", "live_opportunity", "settled_cash"):
            self.assertIn(f'_criterion("{criterion}"', service)
        self.assertNotIn('_ci_gate("public_https"', service)
        self.assertNotIn('_ci_gate("settled_cash"', service)

    def test_final_ci_gate_runs_after_container_and_restore_proofs(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        acceptance = workflow.index("Final V1 acceptance report")
        self.assertGreater(acceptance, workflow.index("Build coding sandbox images and prove isolation surface"))
        self.assertGreater(acceptance, workflow.index("Build production image and prove secrets are excluded"))
        self.assertGreater(acceptance, workflow.index("Encrypted PostgreSQL backup and restore proof"))

    def test_readme_uses_truth_vocabulary_and_safe_defaults(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for marker in ("CODE-COMPLETE", "CI-PROVEN", "EXTERNAL_PROOF_REQUIRED", "Only `SETTLED`", "AUTONOMOUS_MODE=OFF"):
            self.assertIn(marker, readme)


if __name__ == "__main__":
    unittest.main()
