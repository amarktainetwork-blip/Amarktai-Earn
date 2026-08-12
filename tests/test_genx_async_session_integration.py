from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from control.models import Alert, AuditEvent, GenXCall, GenXModelCatalog, Job, JobScore, Marketplace, ModelStat, Worker
from gateways.genx.service import GenXGateway
from workers.genx_support import GenXWorkerError, capability_model_ids, research_with_web


class FakeAsyncSessionClient:
    def __init__(self, *, final=None, wait_error=None, history=None, job_payload=None):
        self.final = final or {
            "job_id": "job-1",
            "model": "gpt-5-nano",
            "status": "completed",
            "result_url": "data/plain;base64,T0s=",
            "usage": {"credits": "0.008"},
        }
        self.wait_error = wait_error
        self.history = history or {"messages": [
            {"role": "user", "content": [{"type": "text", "text": "Reply exactly OK."}]},
            {"role": "assistant", "job_id": "job-1", "content": [{"type": "text", "text": "OK"}]},
        ]}
        self.job_payload = job_payload or self.final
        self.created = 0
        self.messages = 0
        self.waited = []
        self.jobs = []
        self.closed = 0
        self.status_during_wait = ""

    def create_session(self, model, *, system_prompt="", title=""):
        self.created += 1
        return {"session_id": "session-1"}

    def session_message(self, session_id, message, *, idempotency_key="", tools=None, file_ids=None):
        self.messages += 1
        return {"session_id": session_id, "message_id": "message-1", "job_id": "job-1", "status": "queued"}

    def wait(self, job_id, timeout_seconds=180):
        self.waited.append(job_id)
        self.status_during_wait = GenXCall.objects.get(external_job_id=job_id).status
        if self.wait_error:
            raise self.wait_error
        return self.final

    def job(self, job_id):
        self.jobs.append(job_id)
        return self.job_payload

    def session_messages(self, session_id):
        return self.history

    def close_session(self, session_id):
        self.closed += 1
        return {}


class GenXAsyncSessionTruthTests(TestCase):
    def setUp(self):
        market = Marketplace.objects.create(slug="async-genx", display_name="Async GenX")
        self.job = Job.objects.create(
            marketplace=market,
            external_id="async-job",
            title="Async session proof",
            task_class="Research",
            reward="50.00",
            state=Job.State.AWARDED,
        )
        JobScore.objects.create(
            job=self.job,
            p_acquire="1",
            p_accept="1",
            p_payment="1",
            expected_profit="40",
            expected_minutes=10,
            max_genx_credits="2",
        )
        Worker.objects.create(id="async-worker", worker_class="research", version="1", status="READY")
        GenXModelCatalog.objects.create(
            model_id="gpt-5-nano",
            category="text",
            active=True,
            model_payload={"capabilities": ["web_search", "reasoning"]},
        )
        GenXModelCatalog.objects.create(
            model_id="plain-text",
            category="text",
            active=True,
            model_payload={"capabilities": ["reasoning"]},
        )

    def run_session(self, client, request_key="async-request"):
        return GenXGateway(client=client).run_session(
            job_id=self.job.id,
            worker_id="async-worker",
            task_class="research_web",
            system_prompt="Be exact.",
            message="Reply exactly OK.",
            estimated_credits=Decimal("0.25"),
            max_allowed_credits=Decimal("1"),
            request_key=request_key,
            preferred_model="gpt-5-nano",
            preferred_override_reason="DEBUG_PROOF",
        )

    def test_ack_is_polled_and_actual_terminal_usage_is_reconciled_once(self):
        client = FakeAsyncSessionClient()
        call, response = self.run_session(client)

        self.assertEqual(client.waited, ["job-1"])
        self.assertEqual(client.status_during_wait, "SUBMITTED")
        self.assertEqual(call.status, "COMPLETED")
        self.assertEqual(call.credits, Decimal("0.008"))
        self.assertIsNone(call.cost_equivalent)
        self.assertEqual(call.requested_metadata["cost_equivalent_truth"], "UNRESOLVED_VALUATION")
        self.assertEqual(call.result_url, "data/plain;base64,T0s=")
        self.assertEqual(call.requested_metadata["billing_truth"], "ACTUAL")
        self.assertEqual(call.requested_metadata["session_id"], "session-1")
        self.assertEqual(call.requested_metadata["message_id"], "message-1")
        self.assertEqual(call.requested_metadata["remote_job_id"], "job-1")
        self.assertEqual(response["assistant_text"], "OK")
        self.assertEqual(ModelStat.objects.get(model="gpt-5-nano").attempts, 1)

        GenXGateway(client=client).reconcile_remote_job_payload(call.id, client.final, source="OPERATOR_EVIDENCE")
        stat = ModelStat.objects.get(model="gpt-5-nano")
        self.assertEqual(stat.attempts, 1)
        self.assertEqual(stat.credits, Decimal("0.008"))

    def test_completed_without_usage_keeps_reservation_deduplicates_alert_and_accepts_later_usage(self):
        no_usage = {
            "job_id": "job-1",
            "model": "gpt-5-nano",
            "status": "completed",
            "result_url": "data/plain;base64,T0s=",
        }
        client = FakeAsyncSessionClient(final=no_usage, job_payload=no_usage)
        call, response = self.run_session(client, "missing-usage")

        self.assertEqual(call.status, "COMPLETED")
        self.assertEqual(call.credits, Decimal("0"))
        self.assertIsNone(call.cost_equivalent)
        self.assertEqual(call.requested_metadata["billing_truth"], "UNRESOLVED")
        self.assertEqual(response["assistant_text"], "OK")
        self.assertEqual(Alert.objects.filter(alert_type="GENX_USAGE_MISSING", status="OPEN").count(), 1)

        gateway = GenXGateway(client=client)
        gateway.reconcile_pending()
        gateway.reconcile_pending()
        self.assertEqual(Alert.objects.filter(alert_type="GENX_USAGE_MISSING", status="OPEN").count(), 1)
        call.refresh_from_db()
        self.assertEqual(call.requested_metadata["billing_truth"], "UNRESOLVED")

        authoritative = {**no_usage, "usage": {"credits": "0.008"}}
        gateway.reconcile_remote_job_payload(call.id, authoritative, source="OPERATOR_EVIDENCE")
        gateway.reconcile_remote_job_payload(call.id, authoritative, source="OPERATOR_EVIDENCE")
        call.refresh_from_db()
        self.assertEqual(call.credits, Decimal("0.008"))
        self.assertEqual(call.requested_metadata["billing_truth"], "ACTUAL")
        self.assertFalse(Alert.objects.filter(alert_type="GENX_USAGE_MISSING", status="OPEN").exists())
        self.assertEqual(ModelStat.objects.get(model="gpt-5-nano").credits, Decimal("0.008"))
        self.assertEqual(AuditEvent.objects.filter(event_type="genx.billing_reconciled", metadata__call_id=str(call.id)).count(), 1)

        other = GenXCall.objects.create(
            job=self.job,
            model="other",
            status="COMPLETED",
            estimated_credits="0.75",
            requested_metadata={"billing_truth": "UNRESOLVED"},
        )
        from gateways.genx.contracts import effective_reserved_credits
        total = effective_reserved_credits(
            GenXCall.objects.filter(pk=other.pk).values_list("credits", "estimated_credits", "status", "requested_metadata")
        )
        self.assertEqual(total, Decimal("0.75"))

    def test_timeout_after_ack_preserves_all_ids_and_never_replays(self):
        client = FakeAsyncSessionClient(wait_error=TimeoutError("still running"))
        with self.assertRaises(TimeoutError):
            self.run_session(client, "timeout-after-ack")
        call = GenXCall.objects.get(request_key="timeout-after-ack")
        self.assertEqual(call.status, "UNKNOWN_REMOTE_STATE")
        self.assertEqual(call.external_job_id, "job-1")
        self.assertEqual(call.requested_metadata["session_id"], "session-1")
        self.assertEqual(call.requested_metadata["message_id"], "message-1")
        self.assertEqual(call.requested_metadata["remote_job_id"], "job-1")
        self.assertEqual(client.messages, 1)
        self.assertEqual(client.closed, 0)

        replay, _ = self.run_session(client, "timeout-after-ack")
        self.assertEqual(replay.id, call.id)
        self.assertEqual(client.messages, 1)

    def test_authoritative_zero_usage_is_distinct_from_missing_usage(self):
        call = GenXCall.objects.create(
            request_key="actual-zero", job=self.job, worker_id="async-worker",
            model="gpt-5-nano", task_class="research_web", external_job_id="job-zero",
            estimated_credits="0.25", max_allowed_credits="1", status="SUBMITTED",
            requested_metadata={"remote_job_id": "job-zero", "billing_truth": "PENDING"},
        )
        gateway = GenXGateway(client=FakeAsyncSessionClient())
        gateway.reconcile_remote_job_payload(
            call.id,
            {"job_id": "job-zero", "status": "completed", "usage": {"credits": "0"}},
            source="POLL",
        )
        call.refresh_from_db()
        self.assertEqual(call.credits, Decimal("0"))
        self.assertEqual(call.requested_metadata["billing_truth"], "ACTUAL")
        self.assertFalse(Alert.objects.filter(alert_type="GENX_USAGE_MISSING", metadata__call_id=str(call.id)).exists())

        from gateways.genx.contracts import effective_reserved_credits
        reserved = effective_reserved_credits(
            GenXCall.objects.filter(pk=call.pk).values_list("credits", "estimated_credits", "status", "requested_metadata")
        )
        self.assertEqual(reserved, Decimal("0"))

    def test_watchdog_polls_existing_remote_job_without_post(self):
        call = GenXCall.objects.create(
            request_key="watchdog-session",
            job=self.job,
            worker_id="async-worker",
            model="gpt-5-nano",
            task_class="research_web",
            external_job_id="job-1",
            estimated_credits="0.25",
            max_allowed_credits="1",
            status="UNKNOWN_REMOTE_STATE",
            requested_metadata={
                "transport": "session", "session_id": "session-1", "message_id": "message-1",
                "remote_job_id": "job-1", "billing_truth": "PENDING",
            },
        )
        client = FakeAsyncSessionClient()
        result = GenXGateway(client=client).reconcile_pending()
        call.refresh_from_db()
        self.assertEqual(result, {"reconciled": 1, "unresolved": 0})
        self.assertEqual(client.jobs, ["job-1"])
        self.assertEqual(client.messages, 0)
        self.assertEqual(call.status, "COMPLETED")

    def test_historical_session_discovers_one_unambiguous_assistant_job(self):
        recovered = GenXCall.objects.create(
            request_key="historical-one", job=self.job, worker_id="async-worker",
            model="gpt-5-nano", task_class="research_web", external_job_id="session:session-1",
            estimated_credits="0.25", max_allowed_credits="1", status="UNKNOWN_REMOTE_STATE",
            requested_metadata={"transport": "session", "billing_truth": "PENDING"},
        )
        client = FakeAsyncSessionClient()
        GenXGateway(client=client).reconcile_pending()
        recovered.refresh_from_db()
        self.assertEqual(recovered.external_job_id, "job-1")
        self.assertEqual(recovered.requested_metadata["remote_job_id"], "job-1")
        self.assertEqual(client.messages, 0)

    def test_research_capability_filter_uses_provider_truth_and_never_broadens(self):
        self.assertEqual(capability_model_ids("web_search"), ["gpt-5-nano"])
        GenXModelCatalog.objects.filter(model_id="gpt-5-nano").update(
            model_payload={"capabilities": ["reasoning"]}
        )
        with self.assertRaisesRegex(GenXWorkerError, "web_search"):
            capability_model_ids("web_search")
        self.assertEqual(
            capability_model_ids("not-a-capability", fallback_category="text"),
            ["gpt-5-nano", "plain-text"],
        )

    def test_research_worker_consumes_explicit_assistant_text_with_remote_job_identity(self):
        call = SimpleNamespace(
            external_job_id="job-1",
            requested_metadata={"session_id": "session-1", "remote_job_id": "job-1"},
        )
        response = {
            "assistant_text": "Evidence-backed answer.\n\nSources\nhttps://example.com/source",
            "session_history": {"messages": [{
                "role": "assistant", "job_id": "job-1",
                "content": [{"type": "text", "text": "Evidence-backed answer."}],
                "citations": [{"url": "https://example.com/source"}],
            }]},
        }
        captured = {}

        def run_session(**kwargs):
            captured.update(kwargs)
            return call, response

        gateway = SimpleNamespace(run_session=run_session)
        request = SimpleNamespace(
            job_id=self.job.id,
            worker_id="async-worker",
            attempt=1,
            inputs={},
        )
        with patch("workers.genx_support.GenXGateway", return_value=gateway):
            text, sources, returned_call = research_with_web(request, query="current evidence")
        self.assertIn("Evidence-backed answer", text)
        self.assertEqual(sources, ["https://example.com/source"])
        self.assertIs(returned_call, call)
        self.assertEqual(captured["eligible_model_ids"], ["gpt-5-nano"])
        self.assertEqual(captured["tools"], [{"type": "web_search"}])

    def test_ambiguous_historical_session_remains_unresolved(self):
        ambiguous = GenXCall.objects.create(
            request_key="historical-ambiguous", job=self.job, worker_id="async-worker",
            model="gpt-5-nano", task_class="research_web", external_job_id="session:session-2",
            estimated_credits="0.25", max_allowed_credits="1", status="UNKNOWN_REMOTE_STATE",
            requested_metadata={"transport": "session", "billing_truth": "PENDING"},
        )
        client = FakeAsyncSessionClient()
        client.history = {"messages": [
            {"role": "assistant", "job_id": "job-a", "content": "A"},
            {"role": "assistant", "job_id": "job-b", "content": "B"},
        ]}
        result = GenXGateway(client=client).reconcile_pending()
        ambiguous.refresh_from_db()
        self.assertGreaterEqual(result["unresolved"], 1)
        self.assertEqual(ambiguous.external_job_id, "session:session-2")
        self.assertTrue(Alert.objects.filter(
            alert_type="GENX_REMOTE_JOB_ID_AMBIGUOUS", metadata__call_id=str(ambiguous.id)
        ).exists())
        self.assertEqual(client.messages, 0)
