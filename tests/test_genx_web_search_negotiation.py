from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from control.models import AuditEvent, GenXCall, GenXModelCatalog, Job, JobScore, Marketplace
from gateways.genx.client import GenXError
from workers.genx_support import research_web_model_ids, research_with_web


class GenXWebSearchNegotiationTests(TestCase):
    def setUp(self):
        market = Marketplace.objects.create(slug="web-search-negotiation", display_name="Web Search Negotiation")
        self.job = Job.objects.create(
            marketplace=market,
            external_id="web-search-job",
            title="Research current evidence",
            task_class="Research",
            reward="10.00",
            state=Job.State.AWARDED,
        )
        JobScore.objects.create(
            job=self.job,
            p_acquire="1",
            p_accept="1",
            p_payment="1",
            expected_profit="9",
            expected_minutes=5,
            max_genx_credits="2",
        )
        for model_id in ("a-model", "b-model"):
            GenXModelCatalog.objects.create(
                model_id=model_id,
                category="text",
                active=True,
                model_payload={"capabilities": ["reasoning"]},
            )

    def _record_tool_rejection(self, *, model: str, request_key: str) -> GenXCall:
        call = GenXCall.objects.create(
            request_key=request_key,
            job=self.job,
            worker_id="research-worker",
            model=model,
            task_class="research_web",
            external_job_id=f"session:{model}",
            estimated_credits="0.25",
            max_allowed_credits="1",
            status="FAILED",
            credits="0",
            cost_equivalent="0",
            error_code="GenXError",
            requested_metadata={
                "transport": "session",
                "session_id": model,
                "remote_job_id": "",
                "billing_truth": "NOT_APPLICABLE",
                "cost_equivalent_truth": "NOT_APPLICABLE",
            },
        )
        AuditEvent.objects.create(
            severity="ERROR",
            event_type="genx.session_failed",
            actor="genx-gateway",
            metadata={
                "call_id": str(call.id),
                "job_id": str(self.job.id),
                "model": model,
                "phase": "SEND_MESSAGE",
                "http_status": 400,
                "remote_job_id": "",
            },
        )
        return call

    def test_confirmed_zero_cost_tool_rejection_excludes_model_from_future_routing(self):
        self._record_tool_rejection(model="a-model", request_key="rejected-a")

        self.assertEqual(research_web_model_ids(), ["b-model"])

    def test_research_negotiates_past_confirmed_tool_rejection(self):
        captured = []
        successful_call = SimpleNamespace(
            external_job_id="job-b",
            requested_metadata={"session_id": "session-b", "remote_job_id": "job-b"},
        )
        successful_response = {
            "assistant_text": "Evidence-backed answer.\n\nSources\nhttps://example.com/source",
            "session_history": {"messages": [{
                "role": "assistant",
                "job_id": "job-b",
                "content": [{"type": "text", "text": "Evidence-backed answer."}],
                "citations": [{"url": "https://example.com/source"}],
            }]},
        }

        class FakeGateway:
            def run_session(inner_self, **kwargs):
                captured.append(kwargs)
                selected = kwargs["eligible_model_ids"][0]
                if len(captured) == 1:
                    self._record_tool_rejection(model=selected, request_key=kwargs["request_key"])
                    raise GenXError("GenX HTTP 400", status_code=400)
                return successful_call, successful_response

        request = SimpleNamespace(
            job_id=self.job.id,
            worker_id="research-worker",
            attempt=1,
            inputs={},
        )

        with patch("workers.genx_support.GenXGateway", return_value=FakeGateway()):
            text, sources, call = research_with_web(request, query="current evidence")

        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["eligible_model_ids"], ["a-model", "b-model"])
        self.assertEqual(captured[1]["eligible_model_ids"], ["b-model"])
        self.assertNotEqual(captured[0]["request_key"], captured[1]["request_key"])
        self.assertEqual(captured[0]["tools"], [{"type": "web_search"}])
        self.assertEqual(captured[1]["tools"], [{"type": "web_search"}])
        self.assertIn("Evidence-backed answer", text)
        self.assertEqual(sources, ["https://example.com/source"])
        self.assertIs(call, successful_call)

    def test_ambiguous_provider_failure_aborts_without_negotiating_another_model(self):
        captured = []

        class FakeGateway:
            def run_session(inner_self, **kwargs):
                captured.append(kwargs)
                raise GenXError("GenX HTTP 500", status_code=500)

        request = SimpleNamespace(
            job_id=self.job.id,
            worker_id="research-worker",
            attempt=1,
            inputs={},
        )

        with patch("workers.genx_support.GenXGateway", return_value=FakeGateway()):
            with self.assertRaises(GenXError):
                research_with_web(request, query="current evidence")

        self.assertEqual(len(captured), 1)
