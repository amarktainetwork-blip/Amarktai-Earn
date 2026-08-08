import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from markets.agentgigs.client import AgentGigsAdapter, AgentGigsError
from markets.base import NormalizedOpportunity


class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.content = json.dumps(self._payload).encode()
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class AgentGigsClientTests(unittest.TestCase):
    def test_apply_acknowledges_nda_before_documented_apply_endpoint(self):
        session = FakeSession([
            FakeResponse({"accepted": False}),
            FakeResponse({"success": True}),
            FakeResponse({"applicationId": "app-1"}),
        ])
        adapter = AgentGigsAdapter("age_secret", session=session)
        job = NormalizedOpportunity("job-1", "Task", "Research", Decimal("50"))
        result = adapter.apply(job, Decimal("25"), "I can deliver this.")
        self.assertEqual(result["applicationId"], "app-1")
        self.assertEqual([call[1].split(".io")[-1] for call in session.calls], [
            "/api/jobs/job-1/nda",
            "/api/jobs/job-1/nda",
            "/api/jobs/job-1/apply",
        ])
        self.assertEqual(session.calls[-1][2]["json"]["proposed_price"], 2500)
        self.assertEqual(session.calls[-1][2]["json"]["estimated_delivery"], "24 hours")

    def test_apply_falls_back_only_on_definitive_missing_endpoint(self):
        session = FakeSession([
            FakeResponse({"accepted": True}),
            FakeResponse({}, status_code=404),
            FakeResponse({"applicationId": "legacy-app"}),
        ])
        adapter = AgentGigsAdapter("age_secret", session=session)
        job = NormalizedOpportunity("job-1", "Task", "Research", Decimal("50"))
        result = adapter.apply(job, Decimal("25"), "Proposal")
        self.assertEqual(result["applicationId"], "legacy-app")
        self.assertTrue(session.calls[-1][1].endswith("/api/agent/jobs/job-1/accept"))

        session = FakeSession([
            FakeResponse({"accepted": True}),
            FakeResponse({}, status_code=500),
        ])
        adapter = AgentGigsAdapter("age_secret", session=session)
        with self.assertRaises(AgentGigsError):
            adapter.apply(job, Decimal("25"), "Proposal")
        self.assertEqual(len(session.calls), 2)

    def test_discovery_filters_and_clamps_documented_parameters(self):
        session = FakeSession([FakeResponse({"jobs": [], "count": 0})])
        adapter = AgentGigsAdapter("age_secret", session=session)
        adapter.discover_jobs(category="Research", min_budget=500, unsupported="ignored", limit=500)
        params = session.calls[0][2]["params"]
        self.assertNotIn("unsupported", params)
        self.assertEqual(params["category"], "Research")
        self.assertEqual(params["limit"], 100)

    def test_upload_blocks_executable_before_network_upload(self):
        session = FakeSession([FakeResponse({"accepted": True})])
        adapter = AgentGigsAdapter("age_secret", session=session)
        job = NormalizedOpportunity("job-1", "Task", "Data", Decimal("10"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.exe"
            path.write_bytes(b"MZ")
            with self.assertRaises(AgentGigsError):
                adapter.upload_deliverable(job, path)
        self.assertEqual(len(session.calls), 1)

    def test_http_errors_do_not_leak_body_or_api_key_and_capture_retry_after(self):
        session = FakeSession([FakeResponse({"secret": "remote-diagnostic"}, status_code=429, headers={"Retry-After": "17"})])
        adapter = AgentGigsAdapter("age_top_secret", session=session)
        with self.assertRaises(AgentGigsError) as caught:
            adapter.earnings()
        message = str(caught.exception)
        self.assertIn("429", message)
        self.assertEqual(caught.exception.retry_after, 17)
        self.assertNotIn("remote-diagnostic", message)
        self.assertNotIn("age_top_secret", message)


if __name__ == "__main__":
    unittest.main()
