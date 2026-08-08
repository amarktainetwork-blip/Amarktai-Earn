import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "control/models.py"
MIGRATIONS = ROOT / "control/migrations"


class MigrationContractTests(unittest.TestCase):
    def test_migration_chain_covers_all_concrete_control_models(self):
        text = MODELS.read_text(encoding="utf-8")
        models = set(re.findall(r"^class\s+(\w+)\((?:Timestamped|models\.Model)\):", text, flags=re.M))
        models.discard("Timestamped")
        migration_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.py"))
        )
        created = set(re.findall(r'name="([A-Za-z0-9_]+)"', migration_text))
        self.assertEqual(models - created, set())

    def test_job_score_migration_persists_profit_rate_fields(self):
        migration_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.py"))
        )
        for field in ("expected_cash", "expected_profit_per_minute", "expected_profit_per_genx_credit", "max_genx_credits"):
            self.assertIn(field, migration_text)


if __name__ == "__main__":
    unittest.main()
