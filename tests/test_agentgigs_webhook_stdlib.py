import json
import unittest

from markets.agentgigs.webhooks import AgentGigsWebhookError, event_key, parse_webhook, signature_for, verify_signature


class AgentGigsWebhookTests(unittest.TestCase):
    def test_hmac_signature_and_prefix(self):
        raw = b'{"event":"job.accepted","data":{"job":{"id":"j1"}}}'
        signature = signature_for(raw, "secret")
        self.assertTrue(verify_signature(raw, signature, "secret"))
        self.assertTrue(verify_signature(raw, "sha256=" + signature, "secret"))
        self.assertFalse(verify_signature(raw + b"x", signature, "secret"))

    def test_parsing_extracts_job_id_and_event_key_is_stable(self):
        raw = json.dumps({"event": "job.revision_requested", "timestamp": "2026-08-08T00:00:00Z", "data": {"job": {"id": "job-9"}, "message": "Fix it"}}, separators=(",", ":")).encode()
        parsed = parse_webhook(raw)
        self.assertEqual(parsed.external_job_id, "job-9")
        self.assertEqual(event_key(raw), event_key(raw))

    def test_unknown_events_are_rejected(self):
        with self.assertRaises(AgentGigsWebhookError):
            parse_webhook(b'{"event":"admin.secret","data":{}}')


if __name__ == "__main__":
    unittest.main()
