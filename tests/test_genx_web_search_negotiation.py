from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from control.models import AuditEvent, GenXCall, GenXModelCatalog, Job, JobScore, Marketplace, Worker
from gateways.genx.client import GenXError
from workers.genx_support import GenXWorkerError, research_web_model_ids, research_with_web


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
        Worker.objects.create(
            id="research-worker",
            worker_class="research",
            status="READY",
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

    def _record_remote_failure(self, *, model: str, request_key: str, http_status: int) -> tuple[GenXCall, dict]:
        remote_job_id = f"job-{model}"
        call = GenXCall.objects.create(
            request_key=request_key,
            job=self.job,
            worker_id="research-worker",
            model=model,
            task_class="research_web",
            external_job_id=remote_job_id,
            estimated_credits="0.25",
            max_allowed_credits="1",
            status="FAILED",
            credits="0",
            cost_equivalent=None,
            error_code="",
            requested_metadata={
                "transport": "session",
                "session_id": f"session-{model}",
                "message_id": f"message-{model}",
                "remote_job_id": remote_job_id,
                "billing_truth": "UNRESOLVED",
                "cost_equivalent_truth": "ESTIMATED_USAGE",
            },
        )
        response = {
            "assistant_text": "",
            "session_history": {"messages": []},
            "remote_job": {
                "job_id": remote_job_id,
                "model": model,
                "status": "failed",
                "error": f"Provider error ({http_status})",
            },
        }
        return call, response

    def _success(self, *, model: str = "b-model"):
        call = SimpleNamespace(
            model=model,
            status="COMPLETED",
            external_job_id="job-b",
            requested_metadata={"session_id": "session-b", "remote_job_id": "job-b"},
        )
        response = {
            "assistant_text": "Evidence-backed answer.\n\nSources\nhttps://example.com/source",
            "session_history": {"messages": [{
                "role": "assistant",
                "job_id": "job-b",
                "content": [{"type": "text", "text": "Evidence-backed answer."}],
                "citations": [{"url": "https://example.com/source"}],
            }]},
        }
        return call, response

    def test_confirmed_zero_cost_tool_rejection_excludes_model_from_future_routing(self):
        self._record_tool_rejection(model="a-model", request_key="rejected-a")

        self.assertEqual(research_web_model_ids(), ["b-model"])

    def test_research_negotiates_past_confirmed_tool_rejection(self):
        captured = []
        successful_call, successful_response = self._success()

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

    def test_terminal_remote_400_is_persisted_zero_cost_and_negotiates_next_model(self):
        captured = []
        successful_call, successful_response = self._success()
        failed_call = None

        class FakeGateway:
            def run_session(inner_self, **kwargs):
                nonlocal failed_call
                captured.append(kwargs)
                selected = kwargs["eligible_model_ids"][0]
                if len(captured) == 1:
                    failed_call, failed_response = self._record_remote_failure(
                        model=selected,
                        request_key=kwargs["request_key"],
                        http_status=400,
                    )
                    return failed_call, failed_response
                return successful_call, successful_response

        request = SimpleNamespace(
            job_id=self.job.id,
            worker_id="research-worker",
            attempt=3,
            inputs={},
        )

        with patch("workers.genx_support.GenXGateway", return_value=FakeGateway()):
            text, sources, call = research_with_web(request, query="current evidence")

        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["eligible_model_ids"], ["a-model", "b-model"])
        self.assertEqual(captured[1]["eligible_model_ids"], ["b-model"])
        self.assertNotEqual(captured[0]["request_key"], captured[1]["request_key"])
        self.assertIn("Evidence-backed answer", text)
        self.assertEqual(sources, ["https://example.com/source"])
        self.assertIs(call, successful_call)

        failed_call.refresh_from_db()
        self.assertEqual(failed_call.requested_metadata["billing_truth"], "NOT_APPLICABLE")
        self.assertEqual(failed_call.requested_metadata["cost_equivalent_truth"], "NOT_APPLICABLE")
        self.assertEqual(failed_call.requested_metadata["provider_http_status"], 400)
        self.assertEqual(failed_call.cost_equivalent, 0)
        self.assertEqual(failed_call.credits, 0)
        self.assertEqual(failed_call.error_code, "PROVIDER_HTTP_400")
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type="genx.session_remote_tool_rejected",
                metadata__call_id=str(failed_call.id),
                metadata__http_status=400,
            ).exists()
        )
        self.assertEqual(research_web_model_ids(), ["b-model"])

    def test_terminal_remote_500_aborts_without_negotiating_another_model(self):
        captured = []

        class FakeGateway:
            def run_session(inner_self, **kwargs):
                captured.append(kwargs)
                selected = kwargs["eligible_model_ids"][0]
                return self._record_remote_failure(
                    model=selected,
                    request_key=kwargs["request_key"],
                    http_status=500,
                )

        request = SimpleNamespace(
            job_id=self.job.id,
            worker_id="research-worker",
            attempt=3,
            inputs={},
        )

        with patch("workers.genx_support.GenXGateway", return_value=FakeGateway()):
            with self.assertRaisesRegex(GenXWorkerError, "without safe compatibility evidence"):
                research_with_web(request, query="current evidence")

        self.assertEqual(len(captured), 1)
        call = GenXCall.objects.get(request_key=captured[0]["request_key"])
        self.assertEqual(call.requested_metadata["billing_truth"], "UNRESOLVED")
        self.assertIsNone(call.cost_equivalent)
        self.assertFalse(
            AuditEvent.objects.filter(
                event_type="genx.session_remote_tool_rejected",
                metadata__call_id=str(call.id),
            ).exists()
        )

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
