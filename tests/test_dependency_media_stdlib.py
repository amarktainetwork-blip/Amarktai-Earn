import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DependencyMediaContracts(unittest.TestCase):
    def test_dependency_fetch_is_controller_bounded_and_hooks_are_disabled(self):
        broker = (ROOT / "sandbox_broker/server.py").read_text(encoding="utf-8")
        for marker in ("--require-hashes", "--only-binary=:all:", "--ignore-scripts", "DEPENDENCY_MAX_CACHE_BYTES", "DEPENDENCY_MAX_CACHE_FILES", "DEPENDENCY_MAX_CACHE_VOLUMES", "DEPENDENCY_TOTAL_CACHE_QUOTA_BYTES", "dependency manifest changed after verification"):
            self.assertIn(marker, broker)
        self.assertIn(":/opt/amarktai-dependencies:ro", broker)
        self.assertNotIn("GITHUB_TOKEN", broker)
        self.assertNotIn("GENX_API_KEY", broker)

    def test_dependency_preparation_uses_isolated_container_not_host_install(self):
        broker = (ROOT / "sandbox_broker/server.py").read_text(encoding="utf-8")
        service = (ROOT / "control/services/dependencies.py").read_text(encoding="utf-8")
        self.assertIn('"docker", "run"', broker)
        self.assertIn('"--cap-drop", "ALL"', broker)
        self.assertIn('"no-new-privileges:true"', broker)
        self.assertNotIn("subprocess", service)
        self.assertIn("PYTHON_REQUIREMENTS_NOT_HASH_LOCKED", service)

    def test_media_processes_never_shell_concatenate_job_values(self):
        worker = (ROOT / "workers/media/worker.py").read_text(encoding="utf-8")
        self.assertIn("subprocess.run(args", worker)
        self.assertNotIn("shell=True", worker)
        for marker in ("MEDIA_MAX_SOURCE_BYTES", "MEDIA_MAX_OUTPUT_BYTES", "MEDIA_MAX_PIXELS", "MEDIA_MAX_DURATION_SECONDS", "MEDIA_PROCESS_TIMEOUT_SECONDS", "-nostdin"):
            self.assertIn(marker, worker)

    def test_production_image_contains_ffmpeg_and_media_smoke(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("ffmpeg", dockerfile)
        self.assertIn("PYTHONPATH=/app", dockerfile)
        self.assertIn("media-smoke.py", workflow)
        self.assertIn("dependency-prep-smoke.py", workflow)
        self.assertIn("docker-cli", (ROOT / "sandbox/broker.Dockerfile").read_text(encoding="utf-8"))

    def test_dependency_cache_initialization_needs_no_chown_capability(self):
        broker = (ROOT / "sandbox_broker/server.py").read_text(encoding="utf-8")
        self.assertIn('"chmod 1777 /cache"', broker)
        self.assertNotIn('"chown -R 10001:10001 /cache"', broker)


if __name__ == "__main__":
    unittest.main()
