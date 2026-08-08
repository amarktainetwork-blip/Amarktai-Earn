import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MultiFileCompositeContractTests(unittest.TestCase):
    def test_asset_limits_roles_and_composite_bounds_are_explicit(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        for marker in (
            "JOB_ASSET_MAX_FILES=12",
            "JOB_ASSET_MAX_FILE_BYTES=104857600",
            "JOB_ASSET_MAX_TOTAL_BYTES=262144000",
            "JOB_ASSET_MAX_ARCHIVE_MEMBERS=5000",
            "WORKPLAN_MAX_COMPOSITE_STEPS=8",
        ):
            self.assertIn(marker, env)

    def test_asset_policy_blocks_active_content_and_path_escape(self):
        policy = (ROOT / "planning" / "asset_policy.py").read_text(encoding="utf-8")
        self.assertIn("ASSET_ARCHIVE_PATH_UNSAFE", policy)
        self.assertIn("ASSET_ARCHIVE_SYMLINK_BLOCKED", policy)
        self.assertIn("ASSET_ACTIVE_CONTENT_BLOCKED", policy)
        self.assertIn("ASSET_MIME_EXTENSION_MISMATCH", policy)

    def test_composite_execution_requires_upstream_qa(self):
        service = (ROOT / "planning" / "services.py").read_text(encoding="utf-8")
        self.assertIn("downstream step requires QA-passed upstream artifacts", service)
        self.assertIn("COMPOSITE_DEPENDENCY_CYCLE", service)
        self.assertIn("COMPOSITE_STEP_REPAIR_LIMIT", service)
        self.assertIn("output_artifacts.set", service)


if __name__ == "__main__":
    unittest.main()
