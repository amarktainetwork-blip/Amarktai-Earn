import os
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from control.models import (
    Artifact,
    AuditEvent,
    Execution,
    GenXCall,
    GenXModelCatalog,
    Job,
    JobScore,
    Marketplace,
    ModelStat,
    QAResult,
    Worker,
)
from control.ops import agents_snapshot, live_work_snapshot
from gateways.genx.client import GenXError
from gateways.genx.service import GenXGateway
from planning.models import WorkPlan
from planning.services import execute_work_plan, plan_awarded_job, stage_local_job_asset


class Phase8BAgentIntegrationTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(slug="phase8b-market", display_name="Phase 8B Market")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.jobs = self.root / "jobs"
        self.uploads.mkdir(); self.jobs.mkdir()
        self.env = patch.dict(os.environ, {"AMARKTAI_UPLOAD_ROOT": str(self.uploads), "AMARKTAI_JOB_ROOT": str(self.jobs)}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _job(self, external_id: str, title: str, description: str, payload=None):
        normalized = {"description": description}
        if payload:
            normalized.update(payload)
        return Job.objects.create(
            marketplace=self.market,
            external_id=external_id,
            title=title,
            task_class="General",
            reward="20.00",
            state=Job.State.AWARDED,
            normalized_payload=normalized,
        )

    def _stage(self, job, name: str, content: bytes | str):
        path = self.uploads / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return stage_local_job_asset(job_id=job.id, path=str(path), source="phase8b-test")

    def _assert_visible(self, job, worker_class: str):
        agents = agents_snapshot()["rows"]
        self.assertTrue(any(row["worker_class"] == worker_class for row in agents))
        rows = live_work_snapshot()["rows"]
        row = next(item for item in rows if item["job"] == str(job.id))
        self.assertEqual(row["worker"], worker_class)
        self.assertEqual(row["qa"], "PASS")
        self.assertGreaterEqual(row["artifacts"], 1)

    def test_documents_plans_executes_qa_and_is_visible(self):
        job = self._job("doc-1", "Summarize this document", "Summarize the attached document faithfully.")
        self._stage(job, "brief.txt", "This source document contains important facts, numbers, constraints and named entities that must be preserved in summary form.")
        plan = plan_awarded_job(job.id)
        self.assertEqual((plan.worker_class, plan.operation, plan.status), ("documents", "document_summarize", WorkPlan.Status.READY))
        fake_call = SimpleNamespace(model="mock-text-model")
        with patch("workers.documents.worker.generate_text", return_value=("A faithful summary preserving the important facts, numbers, constraints and named entities from the supplied source.", fake_call)):
            plan = execute_work_plan(plan.id)
        self.assertEqual(plan.status, WorkPlan.Status.QA_PASSED)
        self.assertTrue(Artifact.objects.filter(job=job, path__endswith="document.md").exists())
        self._assert_visible(job, "documents")

    def test_research_without_asset_executes_with_sources_and_is_visible(self):
        job = self._job("research-1", "Research current market trends", "Research the current market trends and provide reliable sources.")
        plan = plan_awarded_job(job.id)
        self.assertEqual((plan.worker_class, plan.operation, plan.status), ("research", "research_report", WorkPlan.Status.READY))
        fake_call = SimpleNamespace(model="mock-research-model")
        report = ("Current evidence indicates several market changes that materially affect the opportunity and should be evaluated carefully. " * 2
                  + "\n\nSources\nhttps://example.com/report\nhttps://example.org/data\n")
        with patch("workers.research.worker.research_with_web", return_value=(report, ["https://example.com/report", "https://example.org/data"], fake_call)):
            plan = execute_work_plan(plan.id)
        self.assertEqual(plan.status, WorkPlan.Status.QA_PASSED)
        self._assert_visible(job, "research")

    def test_localization_requires_explicit_language_then_executes_and_is_visible(self):
        job = self._job("loc-1", "Translate this document into Spanish", "Translate the attached document into Spanish without changing facts.")
        self._stage(job, "source.txt", "This is a source document containing factual information that must remain intact during translation.")
        plan = plan_awarded_job(job.id)
        self.assertEqual((plan.worker_class, plan.operation, plan.input_spec["target_language"]), ("localization", "translate_document", "Spanish"))
        fake_call = SimpleNamespace(model="mock-translation-model")
        translated = "Este es un documento de origen con información factual que debe permanecer intacta durante la traducción."
        with patch("workers.localization.worker.generate_text", return_value=(translated, fake_call)):
            plan = execute_work_plan(plan.id)
        self.assertEqual(plan.status, WorkPlan.Status.QA_PASSED)
        self._assert_visible(job, "localization")

        blocked = self._job("loc-2", "Translate this document", "Translate the attached document accurately.")
        self._stage(blocked, "other.txt", "Another document for translation testing.")
        blocked_plan = plan_awarded_job(blocked.id)
        self.assertEqual(blocked_plan.status, WorkPlan.Status.BLOCKED)
        self.assertIn("TARGET_LANGUAGE_NOT_EXPLICIT", blocked_plan.reason_codes)

    def test_transcription_plans_executes_qa_and_is_visible(self):
        job = self._job("transcript-1", "Transcribe this audio", "Transcribe the attached audio accurately.")
        self._stage(job, "audio.mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00")
        plan = plan_awarded_job(job.id)
        self.assertEqual((plan.worker_class, plan.operation, plan.status), ("transcription", "transcribe_media", WorkPlan.Status.READY))
        fake_call = SimpleNamespace(model="mock-transcription-model")
        with patch("workers.transcription.worker.transcribe_media", return_value=("This is the accurately transcribed audio content.", fake_call)):
            plan = execute_work_plan(plan.id)
        self.assertEqual(plan.status, WorkPlan.Status.QA_PASSED)
        self.assertTrue(QAResult.objects.filter(job=job, check_type="transcript_structural", passed=True).exists())
        self._assert_visible(job, "transcription")


class GenXSessionAccountingIntegrationTests(TestCase):
    class FakeGenXClient:
        def __init__(self, *, message_error=None, close_error=None):
            self.created = 0
            self.messages = 0
            self.closed = 0
            self.message_error = message_error
            self.close_error = close_error

        def create_session(self, model, *, system_prompt="", title=""):
            self.created += 1
            return {"id": "session-ci-1"}

        def session_message(self, session_id, message, *, idempotency_key="", tools=None, file_ids=None):
            self.messages += 1
            if self.message_error:
                raise self.message_error
            return {
                "message": {"content": "Research result with sources https://example.com/a https://example.org/b"},
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "billing": {"credits_charged": "0.2000"},
            }

        def session_messages(self, session_id):
            return {"messages": [{"content": "Recovered research result"}], "usage": {"credits": "0.2000"}}

        def close_session(self, session_id):
            self.closed += 1
            if self.close_error:
                raise self.close_error
            return {}

    def setUp(self):
        market = Marketplace.objects.create(slug="genx-session-market", display_name="GenX Session Market")
        self.job = Job.objects.create(
            marketplace=market,
            external_id="genx-session-1",
            title="Research a market",
            task_class="Research",
            reward="50.00",
            state=Job.State.AWARDED,
        )
        JobScore.objects.create(
            job=self.job,
            p_acquire="1.00000",
            p_accept="1.00000",
            p_payment="1.00000",
            expected_profit="40.00",
            expected_minutes=30,
            max_genx_credits="2.0000",
        )
        Worker.objects.create(id="research-ci", worker_class="research", version="1.0.0", status="READY")
        GenXModelCatalog.objects.create(
            model_id="dynamic-text-ci",
            category="text",
            provider="ci",
            active=True,
            price_hint="1.00000000",
            model_payload={"capabilities": ["web_search"]},
        )

    def _run(self, client, request_key):
        return GenXGateway(client=client).run_session(
            job_id=self.job.id,
            worker_id="research-ci",
            task_class="research_web",
            system_prompt="Use web search and cite sources.",
            message="Research the market.",
            estimated_credits=Decimal("0.25"),
            max_allowed_credits=Decimal("1.00"),
            request_key=request_key,
            tools=[{"type": "web_search"}],
        )

    def test_session_call_reserves_budget_records_usage_and_does_not_replay_message(self):
        client = self.FakeGenXClient()
        call, response = self._run(client, "phase8b-session-idempotency")
        self.assertEqual(call.status, "COMPLETED")
        self.assertEqual(call.credits, Decimal("0.2000"))
        self.assertEqual(call.usage, {"input_tokens": 10, "output_tokens": 5})
        self.assertEqual(call.model, "dynamic-text-ci")
        self.assertEqual(call.task_class, "research_web")
        self.assertEqual(call.external_job_id, "session:session-ci-1")
        self.assertEqual(client.created, 1)
        self.assertEqual(client.messages, 1)
        self.assertEqual(client.closed, 1)
        self.assertIn("Research result", str(response))
        stat = ModelStat.objects.get(model="dynamic-text-ci", task_class="research_web")
        self.assertEqual(stat.attempts, 1)
        self.assertEqual(stat.credits, Decimal("0.2000"))
        self.assertTrue(AuditEvent.objects.filter(event_type="genx.call_reserved").exists())
        self.assertTrue(AuditEvent.objects.filter(event_type="genx.call_reconciled").exists())

        replay, _ = self._run(client, "phase8b-session-idempotency")
        self.assertEqual(replay.id, call.id)
        self.assertEqual(client.created, 1)
        self.assertEqual(client.messages, 1)
        self.assertEqual(GenXCall.objects.filter(request_key="phase8b-session-idempotency").count(), 1)

    def test_message_validation_rejections_are_failed_with_session_identity_retained(self):
        for status_code in (400, 422):
            with self.subTest(status_code=status_code):
                request_key = f"phase8b-message-rejected-{status_code}"
                client = self.FakeGenXClient(
                    message_error=GenXError("deterministic validation rejection", status_code=status_code)
                )

                with self.assertRaises(GenXError):
                    self._run(client, request_key)

                call = GenXCall.objects.get(request_key=request_key)
                self.assertEqual(call.status, "FAILED")
                self.assertIsNotNone(call.completed_at)
                self.assertEqual(call.external_job_id, "session:session-ci-1")
                self.assertEqual(call.credits, Decimal("0"))
                self.assertEqual(client.created, 1)
                self.assertEqual(client.messages, 1)
                event = AuditEvent.objects.get(event_type="genx.session_failed", metadata__call_id=str(call.id))
                self.assertEqual(event.metadata["phase"], "SEND_MESSAGE")
                self.assertEqual(event.metadata["http_status"], status_code)

    def test_message_rate_limit_remains_unknown_without_replay(self):
        client = self.FakeGenXClient(message_error=GenXError("rate limited", status_code=429))

        with self.assertRaises(GenXError):
            self._run(client, "phase8b-message-rate-limited")

        call = GenXCall.objects.get(request_key="phase8b-message-rate-limited")
        self.assertEqual(call.status, "UNKNOWN_REMOTE_STATE")
        self.assertIsNone(call.completed_at)
        self.assertEqual(call.external_job_id, "session:session-ci-1")
        self.assertEqual(call.credits, Decimal("0"))
        self.assertEqual(client.messages, 1)
        event = AuditEvent.objects.get(
            event_type="genx.session_unknown_remote_state",
            metadata__call_id=str(call.id),
        )
        self.assertEqual(event.metadata["phase"], "SEND_MESSAGE")
        self.assertEqual(event.metadata["http_status"], 429)

    def test_timeout_and_network_delivery_ambiguity_remain_unknown_without_replay(self):
        cases = (
            ("timeout", TimeoutError("timed out after send")),
            ("network", GenXError("connection lost after send")),
        )
        for name, error in cases:
            with self.subTest(name=name):
                request_key = f"phase8b-message-ambiguous-{name}"
                client = self.FakeGenXClient(message_error=error)

                with self.assertRaises(error.__class__):
                    self._run(client, request_key)

                call = GenXCall.objects.get(request_key=request_key)
                self.assertEqual(call.status, "UNKNOWN_REMOTE_STATE")
                self.assertIsNone(call.completed_at)
                self.assertEqual(call.external_job_id, "session:session-ci-1")
                self.assertEqual(call.credits, Decimal("0"))
                self.assertEqual(client.messages, 1)
                event = AuditEvent.objects.get(
                    event_type="genx.session_unknown_remote_state",
                    metadata__call_id=str(call.id),
                )
                self.assertEqual(event.metadata["phase"], "SEND_MESSAGE")

    def test_close_failure_preserves_completed_call_and_emits_warning(self):
        client = self.FakeGenXClient(close_error=GenXError("close failed", status_code=503))

        call, response = self._run(client, "phase8b-session-close-failure")

        self.assertEqual(call.status, "COMPLETED")
        self.assertEqual(call.credits, Decimal("0.2000"))
        self.assertIn("Research result", str(response))
        self.assertEqual(client.messages, 1)
        self.assertEqual(client.closed, 1)
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type="genx.session_close_failed",
                severity="WARNING",
                metadata__call_id=str(call.id),
            ).exists()
        )
