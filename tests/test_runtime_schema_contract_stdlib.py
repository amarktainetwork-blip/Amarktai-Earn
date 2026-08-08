import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RuntimeSchemaContractTests(unittest.TestCase):
    def test_second_migration_contains_required_runtime_economic_entities(self):
        text = (ROOT / "control/migrations/0002_runtime_economics_genx.py").read_text(encoding="utf-8")
        required = [
            "MarketplaceCredential",
            "MarketPolicyVersion",
            "MarketHealth",
            "PayoutAccount",
            "Application",
            "Bid",
            "Claim",
            "JobMessage",
            "Node",
            "WorkerVersion",
            "Execution",
            "Artifact",
            "GenXModelCatalog",
            "GenXAccountSnapshot",
            "ModelStat",
            "TreasuryBalance",
            "Alert",
            "SystemSetting",
        ]
        for model in required:
            with self.subTest(model=model):
                self.assertIn(f'name="{model}"', text)
        for field in ("max_genx_credits", "estimated_credits", "max_allowed_credits", "request_key", "result_url"):
            with self.subTest(field=field):
                self.assertIn(f'name="{field}"', text)

    def test_models_keep_credentials_encrypted_and_job_money_truth_separate(self):
        text = (ROOT / "control/models.py").read_text(encoding="utf-8")
        self.assertIn("encrypted_value = models.TextField()", text)
        self.assertIn('SETTLED = "SETTLED"', text)
        self.assertIn("class TreasuryBalance", text)
        self.assertIn("class GenXModelCatalog", text)


if __name__ == "__main__":
    unittest.main()
