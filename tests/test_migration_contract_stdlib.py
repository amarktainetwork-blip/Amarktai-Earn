import re
import unittest
from pathlib import Path


class MigrationContractTests(unittest.TestCase):
    def test_initial_migration_covers_all_concrete_control_models(self):
        root = Path(__file__).resolve().parents[1]
        model_source = (root / "control/models.py").read_text(encoding="utf-8")
        migration_source = (root / "control/migrations/0001_initial.py").read_text(encoding="utf-8")
        models = {
            name for name in re.findall(r"^class (\w+)\(", model_source, flags=re.MULTILINE)
            if name != "Timestamped"
        }
        created = set(re.findall(r'name="(\w+)"', migration_source))
        self.assertEqual(models - created, set())

    def test_job_score_migration_persists_profit_rate_fields(self):
        source = (Path(__file__).resolve().parents[1] / "control/migrations/0001_initial.py").read_text(encoding="utf-8")
        self.assertIn('"expected_profit_per_minute"', source)
        self.assertIn('"expected_profit_per_genx_credit"', source)
        self.assertIn('"reason_codes"', source)


if __name__ == "__main__":
    unittest.main()
