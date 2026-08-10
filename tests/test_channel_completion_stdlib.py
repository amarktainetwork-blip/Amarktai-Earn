import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from control.services.autonomy import AutonomyMode, acquisition_autonomy, current_mode


ROOT = Path(__file__).resolve().parents[1]


class ChannelCompletionStdlibTests(unittest.TestCase):
    def test_manual_mode_never_grants_automatic_acquisition(self):
        with patch.dict(os.environ, {"AUTONOMOUS_MODE": "MANUAL"}, clear=False):
            self.assertEqual(current_mode(), AutonomyMode.MANUAL)
            decision = acquisition_autonomy(switch_enabled=True)
            self.assertFalse(decision.may_acquire)
            self.assertIn("AUTONOMY_MANUAL_ONLY", decision.reason_codes)

    def test_startup_reapplies_onboarding_and_commercial_pricing_in_order(self):
        script = (ROOT / "scripts" / "start-web.sh").read_text()
        expected = [
            "python manage.py production_check",
            "python manage.py migrate --noinput",
            "python manage.py bootstrap_revenue_catalog",
            "python manage.py bootstrap_channel_packages",
            "python manage.py bootstrap_channel_onboarding",
            "python manage.py bootstrap_channel_commercial_pricing",
            "python manage.py collectstatic --noinput",
            "exec gunicorn",
        ]
        offsets = [script.index(item) for item in expected]
        self.assertEqual(offsets, sorted(offsets))

    def test_watcher_is_unified_revenue_watcher_not_agentgigs_only_loop(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("exec python manage.py run_revenue_watcher", compose)
        self.assertNotIn("exec python manage.py run_agentgigs_watcher", compose)

    def test_public_channel_ingress_defaults_fail_closed(self):
        example = (ROOT / ".env.example").read_text()
        self.assertIn("RAPIDAPI_PUBLIC_INGRESS_ENABLED=0", example)
        self.assertIn("LEMON_SQUEEZY_WEBHOOK_ENABLED=0", example)
        self.assertIn("INBOUND_SERVICE_AUTO_ACCEPT_ENABLED=0", example)
        self.assertIn("AUTONOMOUS_MODE=OFF", example)

    def test_apify_actor_bundle_matches_bounded_offhost_contract(self):
        actor_path = ROOT / "integrations" / "apify_actor" / ".actor" / "actor.json"
        actor = json.loads(actor_path.read_text())
        self.assertEqual(actor["actorSpecification"], 1)
        self.assertEqual(actor["version"], "0.1")
        self.assertEqual(actor["dockerfile"], "./Dockerfile")
        self.assertEqual(actor["input"], "./input_schema.json")
        self.assertEqual(actor["output"], "./output_schema.json")
        runtime = (ROOT / "integrations" / "apify_actor" / "src" / "main.py").read_text()
        self.assertIn("if not ip.is_global", runtime)
        self.assertIn("robots.txt", runtime)
        self.assertIn("MAX_BYTES", runtime)
        self.assertIn("Actor.push_data", runtime)
        self.assertNotIn("POSTGRES", runtime)
        self.assertNotIn("REDIS", runtime)

    def test_production_preflight_knows_manual_mode_and_public_auto_accept_gate(self):
        source = (ROOT / "control" / "management" / "commands" / "production_check.py").read_text()
        self.assertIn('"MANUAL"', source)
        self.assertIn("INBOUND_SERVICE_AUTO_ACCEPT_ENABLED", source)
        self.assertIn('mode not in {"LOW_RISK", "FULL"}', source)

    def test_exports_never_claim_external_mutation(self):
        source = (ROOT / "control" / "services" / "channel_exports.py").read_text()
        self.assertIn('"external_mutation_allowed": False', source)
        self.assertIn('"publication_ready": False', source)
        self.assertIn("X-RapidAPI-Proxy-Secret", source)
        self.assertIn("X-RapidAPI-User", source)
        self.assertIn("HMAC-SHA256", source)
        self.assertIn("ASYNC_202_POLL_ARTIFACT", source)


if __name__ == "__main__":
    unittest.main()
