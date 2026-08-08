import unittest
from pathlib import Path

from workers.registry import all_specs, operation_spec, registry_manifest, supports_operation

ROOT = Path(__file__).resolve().parents[1]


class WorkerRegistryDashboardContractTests(unittest.TestCase):
    def test_registry_owns_operation_routing(self):
        specs = all_specs()
        self.assertGreaterEqual(len(specs), 1)
        structured = operation_spec("json_to_csv")
        self.assertEqual(structured.worker_class, "structured_data")
        self.assertTrue(supports_operation("structured_data", "csv_normalize"))
        self.assertFalse(supports_operation("structured_data", "research_report"))
        manifest = registry_manifest()
        self.assertEqual(manifest[0]["worker_class"], "structured_data")
        self.assertIn("qa_profile", manifest[0])

    def test_executor_persists_registry_identity_and_common_qa(self):
        execution = (ROOT / "control" / "services" / "execution.py").read_text(encoding="utf-8")
        self.assertIn("spec.worker_class", execution)
        self.assertIn("spec.version", execution)
        self.assertIn("run_qa(spec.qa_profile", execution)
        self.assertIn('"worker_class": spec.worker_class', execution)
        self.assertIn('"operation": operation', execution)

    def test_dashboard_has_all_required_operations_sections(self):
        ops = (ROOT / "control" / "ops.py").read_text(encoding="utf-8")
        template = (ROOT / "control" / "templates" / "control" / "operations.html").read_text(encoding="utf-8")
        urls = (ROOT / "config" / "urls.py").read_text(encoding="utf-8")
        for section in (
            "overview", "live-work", "agents", "markets", "earnings", "treasury", "genx",
            "nodes", "storage", "performance", "logs", "alerts", "settings", "security",
        ):
            self.assertIn(f'"{section}"', ops)
        self.assertIn("registry_manifest", ops)
        self.assertIn("QAResult", ops)
        self.assertIn("GenXCall", ops)
        self.assertIn("/api/ops/", template)
        self.assertIn("ops/<slug:section>/", urls)

    def test_dashboard_never_exposes_sensitive_setting_values(self):
        ops = (ROOT / "control" / "ops.py").read_text(encoding="utf-8")
        self.assertIn('"CONFIGURED — HIDDEN" if row.sensitive else row.value', ops)
        self.assertNotIn("totp_secret_encrypted", ops)
        self.assertNotIn("encrypted_value", ops)


if __name__ == "__main__":
    unittest.main()
