from __future__ import annotations

import json
import unittest
from decimal import Decimal

from markets.algora.client import AlgoraAdapter
from markets.agentgigs.client import AgentGigsAdapter
from markets.base import MarketCapabilities, NormalizedOpportunity
from markets.callboard.client import CallboardAdapter
from markets.catalog import BY_SLUG
from markets.dealwork.client import DealworkAdapter
from markets.opire.client import OpireAdapter
from markets.taskbounty.client import TaskBountyAdapter


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = payload if payload is not None else {}
        self.status_code = status
        self.ok = 200 <= status < 300
        self.content = json.dumps(self.payload).encode()
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class FakeMCP:
    def __init__(self):
        self.calls = []

    def list_tools(self):
        return {"search_jobs", "claim_job", "place_bid", "submit_deliverable", "get_contract", "get_balance", "get_my_channels", "send_contract_event"}

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "search_jobs":
            return {"jobs": [{"id": "dw-1", "title": "Build report", "budgetMax": "75", "category": "Research"}]}
        return {"id": f"{name}-1", **arguments}


class MarketAdapterContractTests(unittest.TestCase):
    def test_common_capability_contract_has_every_required_flag(self):
        expected = {
            "discover", "normalize", "claim", "apply", "bid", "messages", "input_assets",
            "submission", "revision", "status", "payment", "payout",
            "webhook_or_event_support", "rate_limit", "policy_verified", "payout_ready",
        }
        self.assertEqual(set(MarketCapabilities().as_dict()), expected)
        self.assertEqual(set(BY_SLUG), {"agentgigs", "dealwork", "callboard", "opire", "algora", "taskbounty"})
        for definition in BY_SLUG.values():
            self.assertEqual(set(definition.capabilities.as_dict()), expected)
            self.assertFalse(definition.capabilities.payout_ready)
            self.assertTrue(definition.source_urls)
            self.assertIn("SOUTH_AFRICA", " ".join(definition.blockers))

    def test_callboard_official_discovery_application_assets_and_submission_paths(self):
        session = FakeSession([
            FakeResponse({"jobs": [{"id": "cb-1", "title": "Research", "payCents": 5000, "jobTypeKey": "research"}]}),
            FakeResponse({"id": "app-1"}),
            FakeResponse({"files": [{"id": "f1", "downloadUrl": "https://example.test/a"}]}),
            FakeResponse({"id": "sub-1"}),
        ])
        adapter = CallboardAdapter("secret", session=session)
        jobs = adapter.discover_jobs(search="research", limit=999)
        job = adapter.normalize_job(jobs[0])
        self.assertEqual(job.reward, Decimal("50"))
        adapter.apply(job, Decimal("50"), "ignored because official API-key endpoint has no body")
        adapter.get_input_assets("slot-1")
        adapter.submit(job, {"participation_slot_id": "slot-1", "market_payload": {"artifactUrl": "https://example.test/out"}})
        self.assertTrue(session.calls[0][1].endswith("/api/v2/jobs/search"))
        self.assertEqual(session.calls[0][2]["params"]["limit"], 100)
        self.assertTrue(session.calls[1][1].endswith("/api/v2/jobs/cb-1/applications"))
        self.assertTrue(session.calls[2][1].endswith("/api/v2/participation-slots/slot-1/input-files"))
        self.assertTrue(session.calls[3][1].endswith("/api/v2/participation-slots/slot-1/submit"))

    def test_callboard_never_guesses_submission_schema(self):
        adapter = CallboardAdapter("secret", session=FakeSession([]))
        job = NormalizedOpportunity("cb-1", "Task", "research", Decimal("10"))
        with self.assertRaisesRegex(ValueError, "market_payload"):
            adapter.submit(job, {"participation_slot_id": "slot-1", "url": "https://example.test/out"})

    def test_dealwork_official_rest_job_bid_claim_and_submit_flow(self):
        session = FakeSession([
            FakeResponse({
                "data": [{
                    "id": "dw-1",
                    "title": "Build report",
                    "description": "Analyze the supplied dataset and produce a concise report.",
                    "budgetMax": "75",
                    "category": "Research",
                    "acceptanceCriteria": [{"id": "c1", "description": "Report complete"}],
                }],
                "meta": {"total": 1, "page": 1, "per_page": 5},
            }),
            FakeResponse({"data": {"id": "bid-1"}}, status=201),
            FakeResponse({"data": {"id": "claim-contract"}}, status=201),
            FakeResponse({"data": {"id": "deliverable-1", "version": 1}}, status=201),
            FakeResponse({"data": {"previousState": "in_progress", "newState": "in_review"}}),
        ])

        adapter = DealworkAdapter("secret", session=session)

        raw = adapter.discover_jobs(limit=5)[0]
        job = adapter.normalize_job(raw)
        self.assertEqual(job.reward, Decimal("75"))

        adapter.bid(job, Decimal("60"))
        adapter.claim(job)

        contract_job = NormalizedOpportunity(
            job.external_id,
            job.title,
            job.task_class,
            job.reward,
            raw={**raw, "contractId": "c-1"},
        )

        result = adapter.submit(
            contract_job,
            {
                "url": "https://example.test/report.pdf",
                "notes": "QA passed",
            },
        )

        self.assertEqual(session.calls[0][0], "GET")
        self.assertTrue(session.calls[0][1].endswith("/api/v1/jobs"))
        self.assertEqual(session.calls[0][2]["params"]["per_page"], 5)

        bid = session.calls[1]
        self.assertEqual(bid[0], "POST")
        self.assertTrue(bid[1].endswith("/api/v1/jobs/dw-1/bids"))
        self.assertEqual(bid[2]["json"]["proposedAmount"], "60.00")
        self.assertGreater(bid[2]["json"]["estimatedHours"], 0)
        self.assertIn("Build report", bid[2]["json"]["proposalText"])

        claim = session.calls[2]
        self.assertTrue(claim[1].endswith("/api/v1/jobs/dw-1/claim"))
        self.assertEqual(claim[2]["json"]["acceptedCriteriaIds"], ["c1"])

        deliverable = session.calls[3]
        self.assertTrue(
            deliverable[1].endswith(
                "/api/v1/contracts/c-1/deliverables"
            )
        )
        self.assertEqual(
            deliverable[2]["json"]["outputData"]["artifactUrl"],
            "https://example.test/report.pdf",
        )

        submit = session.calls[4]
        self.assertTrue(
            submit[1].endswith("/api/v1/contracts/c-1/events")
        )
        self.assertEqual(submit[2]["json"]["type"], "SUBMIT_WORK")
        self.assertEqual(
            submit[2]["json"]["deliverableId"],
            "deliverable-1",
        )

        self.assertEqual(
            result["submission"]["newState"],
            "in_review",
        )

    def test_taskbounty_rest_flow_and_crypto_fail_closed_truth(self):
        session = FakeSession([
            FakeResponse({"tasks": [{"id": "tb-1", "title": "Fix bug", "reward": 40, "language": "Python"}]}),
            FakeResponse({"clone_url": "https://example.test/clone"}),
            FakeResponse({"id": "submission-1"}),
        ])
        adapter = TaskBountyAdapter("tb_live_secret", session=session)
        job = adapter.normalize_job(adapter.discover_jobs(limit=1)[0])
        adapter.get_input_assets(job)
        adapter.submit(job, {"external_link": "https://github.com/acme/repo/pull/12"})
        self.assertEqual(job.reward, Decimal("40"))
        self.assertTrue(adapter.payout_status()["crypto_prohibited"])
        self.assertFalse(adapter.payout_status()["ready"])
        with self.assertRaises(ValueError):
            adapter.submit(job, {"external_link": "https://evil.test/not-a-pr"})

    def test_official_workflow_sources_normalize_but_never_claim_settlement(self):
        opire = OpireAdapter(lambda **_: [{"id": "op-1", "title": "Issue", "reward": 20}])
        algora = AlgoraAdapter(lambda **_: [{"id": "al-1", "task": {"title": "Issue", "repo_name": "org/repo"}, "reward": 100}])
        opire_job = opire.normalize_job(opire.discover_jobs()[0])
        algora_job = algora.normalize_job(algora.discover_jobs()[0])
        self.assertEqual(opire_job.reward, Decimal("20"))
        self.assertEqual(algora_job.title, "Issue")
        self.assertFalse(opire.get_status(opire_job)["settled"])
        self.assertFalse(algora.get_status(algora_job)["settled"])

    def test_agentgigs_expands_only_evidenced_operations_and_reads_real_reputation(self):
        session = FakeSession([FakeResponse({"reputation": {"rating": 4.9, "completedJobs": 52}})])
        adapter = AgentGigsAdapter("age_secret", session=session)
        proven = adapter.normalize_job({
            "id": "ag-1", "title": "Create a cited research report", "description": "Include sources",
            "category": "Research", "budget_max": 5000,
        })
        vague = adapter.normalize_job({
            "id": "ag-2", "title": "Help us", "description": "Various tasks",
            "category": "Research", "budget_max": 5000,
        })
        self.assertEqual(proven.raw["operation"], "research_report")
        self.assertNotIn("operation", vague.raw)
        reputation = adapter.reputation("agent-1")
        self.assertEqual(reputation["reputation"]["completedJobs"], 52)
        self.assertTrue(session.calls[0][1].endswith("/api/public/agents/agent-1/reputation"))


if __name__ == "__main__":
    unittest.main()
