import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OperationsGovernorContractTests(unittest.TestCase):
    def test_fail_closed_resource_defaults_and_retention_are_explicit(self):
        env = (ROOT / ".env.example").read_text(encoding="utf-8")
        for marker in (
            "AMARKTAI_MIN_FREE_DISK_BYTES=2147483648",
            "AMARKTAI_MIN_FREE_DISK_PERCENT=10",
            "AMARKTAI_MIN_MEMORY_HEADROOM_BYTES=536870912",
            "MAX_ACTIVE_CODE_SANDBOXES=1",
            "MAX_ACTIVE_MEDIA_PROCESSES=1",
            "WATCHDOG_INTERVAL_SECONDS=60",
            "RETENTION_OPERATIONAL_LOG_DAYS=14",
        ):
            self.assertIn(marker, env)

    def test_watchdog_restart_and_boot_dependencies_are_declared(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        watchdog = compose.split("  watchdog:", 1)[1].split("  genx-proxy:", 1)[0]
        self.assertIn("restart: unless-stopped", watchdog)
        self.assertIn("condition: service_completed_successfully", watchdog)
        self.assertIn("condition: service_healthy", watchdog)
        self.assertIn("python manage.py run_watchdog", watchdog)

    def test_ambiguous_remote_mutations_are_reconciled_not_replayed(self):
        source = (ROOT / "control" / "services" / "recovery.py").read_text(encoding="utf-8")
        self.assertIn("AMBIGUOUS_EXTERNAL_MUTATION", source)
        self.assertIn("UNKNOWN_REMOTE_STATE", source)
        self.assertIn("BLOCK_BLIND_REPLAY", source)
        self.assertNotIn("client.generate(", source)

    def test_sandbox_cleanup_remains_broker_owned(self):
        broker = (ROOT / "sandbox_broker" / "server.py").read_text(encoding="utf-8")
        self.assertIn('"/cleanup"', broker)
        self.assertIn("label=amarktai.sandbox=true", broker)
        self.assertIn('"docker", "rm", "-f"', broker)
        recovery = (ROOT / "control" / "services" / "recovery.py").read_text(encoding="utf-8")
        self.assertNotIn("/var/run/docker.sock", recovery)


if __name__ == "__main__":
    unittest.main()
