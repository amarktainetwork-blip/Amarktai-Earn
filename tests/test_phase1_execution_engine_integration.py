from __future__ import annotations

import base64
import os
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from control.models import (
    Artifact,
    AuditEvent,
    Execution,
    GenXCall,
    GenXCreditValuation,
    Job,
    JobScore,
    Marketplace,
    ModelStat,
    QAResult,
    Submission,
    Worker,
)
from control.services.genx_recovery import GenXRecoveryError, recover_completed_genx_call
from gateways.genx.service import GenXGateway
from planning.models import AcceptanceEvaluation, WorkPlan


REPORT = """# TaskBounty Research Report

The research identified a practical opportunity and cross-checked the available evidence.
The first source is https://example.com/source-one and the second independent source is
https://example.org/source-two. The evidence is sufficient for a substantive cited report,
and this paragraph deliberately provides enough content for the deterministic research QA gate.

## Sources
- https://example.com/source-one
- https://example.org/source-two
"""


class ReadOnlyCompletedGenXClient:
    def __init__(self, *, status="completed"):
        self.status = status
        self.read_calls = []
        self.mutation_calls = []
        encoded = base64.b64encode(REPORT.encode("utf-8")).decode("ascii")
        self.payload = {
            "job_id": "gnxsh_job_phase1_completed",
            "model": "claude-opus-4-8",
            "status": status,
            "result_url": f"data:text/plain;base64,{encoded}",
            "usage": {"credits": "89.721"},
        }

    def job(self, job_id):
        self.read_calls.append(("job", job_id))
        return dict(self.payload)

    def session_messages(self, session_id):
        self.read_calls.append(("session_messages", session_id))
        return {
            "messages": [
                {
                    "role": "assistant",
                    "job_id": "gnxsh_job_phase1_completed",
                    "message_id": "gnxsh_message_phase1",
                    "content": REPORT,
                }
            ]
        }

    def result(self, job_id):
        self.read_calls.append(("result", job_id))
        return {"text": REPORT}

    def job_file(self, job_id, *, max_bytes):
        self.read_calls.append(("job_file", job_id, max_bytes))
        return REPORT.encode("utf-8")

    def _mutation(self, name):
        self.mutation_calls.append(name)
        raise AssertionError(f"provider mutation {name} must never be called during recovery")

    def generate(self, *args, **kwargs):
        return self._mutation("generate")

    def create_session(self, *args, **kwargs):
        return self._mutation("create_session")

    def session_message(self, *args, **kwargs):
        return self._mutation("session_message")

    def close_session(self, *args, **kwargs):
        return self._mutation("close_session")

    def cancel(self, *args, **kwargs):
        return self._mutation("cancel")

    def upload_file(self, *args, **kwargs):
        return self._mutation("upload_file")

    def delete_file(self, *args, **kwargs):
        return self._mutation("delete_file")


class Phase1CompletedProviderRecoveryIntegrationTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(
            os.environ,
            {
                "AMARKTAI_JOB_ROOT": str(self.root),
                "AUTONOMOUS_MODE": "OFF",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

        market = Marketplace.objects.create(slug="phase1-recovery", display_name="Phase 1 Recovery")
        self.job = Job.objects.create(
            marketplace=market,
            external_id="phase1-existing-paid-research",
            title="Research TaskBounty with citations",
            task_class="Research",
            reward="1000.00",
            currency="USD",
            state=Job.State.FAILED,
            normalized_payload={
                "operation": "research_report",
                "inputs": {"query": "TaskBounty", "requirements": "Cite sources"},
            },
        )
        JobScore.objects.create(
            job=self.job,
            p_acquire="1",
            p_accept="1",
            p_payment="1",
            expected_profit="900",
            expected_minutes=10,
            max_genx_credits="100",
        )
        self.worker = Worker.objects.create(
            id="research-phase1-worker",
            worker_class="research",
            version="1.0.0",
            status="ERROR",
        )
        self.workspace = self.root / str(self.job.id) / "attempt-1"
        self.workspace.mkdir(parents=True)
        started = timezone.now() - timedelta(minutes=5)
        self.execution = Execution.objects.create(
            job=self.job,
            worker=self.worker,
            attempt=1,
            status="FAILED",
            workspace=str(self.workspace),
            started_at=started,
            ended_at=timezone.now() - timedelta(minutes=2),
            error_code="JobTimeoutException",
            error_detail="Task exceeded maximum timeout value (180 seconds)",
        )
        self.plan = WorkPlan.objects.create(
            job=self.job,
            worker_class="research",
            operation="research_report",
            input_spec={
                "operation": "research_report",
                "query": "TaskBounty",
                "requirements": "Cite sources",
            },
            status=WorkPlan.Status.FAILED,
            execution_attempts=1,
            last_error_code="JobTimeoutException",
            reason_codes=["OUTER_RQ_TIMEOUT"],
        )
        GenXCreditValuation.objects.create(
            version="phase1-test-usd",
            currency="USD",
            monetary_cost_per_credit=Decimal("0.0100000000"),
            source="TEST_AUTHORITATIVE_VALUATION",
            evidence={"fixture": True},
            effective_at=timezone.now() - timedelta(hours=1),
            verified=True,
            active=True,
        )
        self.call = GenXCall.objects.create(
            request_key="phase1-existing-paid-call",
            job=self.job,
            worker=self.worker,
            model="claude-opus-4-8",
            task_class="research_web",
            external_job_id="gnxsh_job_phase1_completed",
            estimated_credits="0.25",
            max_allowed_credits="100",
            credits="0",
            status="SUBMITTED",
            requested_metadata={
                "transport": "session",
                "session_id": "gnxsh_session_phase1",
                "message_id": "gnxsh_message_phase1",
                "remote_job_id": "gnxsh_job_phase1_completed",
                "billing_truth": "PENDING",
                "cost_equivalent_truth": "ESTIMATED",
            },
            started_at=started + timedelta(seconds=20),
        )

    def test_existing_paid_success_recovers_same_attempt_artifact_qa_acceptance_and_economics_once(self):
        client = ReadOnlyCompletedGenXClient()
        gateway = GenXGateway(client=client)

        first = recover_completed_genx_call(
            self.call.id,
            expected_remote_job_id="gnxsh_job_phase1_completed",
            gateway=gateway,
        )
        second = recover_completed_genx_call(
            self.call.id,
            expected_remote_job_id="gnxsh_job_phase1_completed",
            gateway=gateway,
        )

        self.call.refresh_from_db()
        self.execution.refresh_from_db()
        self.plan.refresh_from_db()
        self.job.refresh_from_db()
        self.worker.refresh_from_db()

        self.assertEqual(client.mutation_calls, [])
        self.assertEqual(self.call.status, "COMPLETED")
        self.assertEqual(self.call.credits, Decimal("89.7210"))
        self.assertEqual(self.call.cost_equivalent, Decimal("0.8972"))
        self.assertEqual(self.call.requested_metadata["billing_truth"], "ACTUAL")
        self.assertTrue(self.call.requested_metadata["recovered_without_provider_replay"])

        self.assertEqual(Execution.objects.filter(job=self.job).count(), 1)
        self.assertEqual(self.execution.attempt, 1)
        self.assertEqual(self.execution.status, "QA_PASSED")
        self.assertEqual(self.execution.error_code, "")
        self.assertEqual(self.plan.execution_attempts, 1)
        self.assertEqual(self.plan.status, WorkPlan.Status.QA_PASSED)
        self.assertEqual(self.job.state, Job.State.EXECUTING)
        self.assertEqual(self.worker.status, "READY")

        artifacts = Artifact.objects.filter(execution=self.execution)
        self.assertEqual(artifacts.count(), 1)
        artifact = artifacts.get()
        self.assertEqual(Path(artifact.path).name, "research-report.md")
        self.assertTrue(Path(artifact.path).is_file())
        self.assertGreater(artifact.size_bytes, 100)
        self.assertEqual(len(artifact.sha256), 64)
        self.assertTrue(artifact.accepted)
        self.assertIn("text/", artifact.mime_type)

        self.assertEqual(QAResult.objects.filter(execution=self.execution).count(), 1)
        qa = QAResult.objects.get(execution=self.execution)
        self.assertTrue(qa.passed)
        evaluation = AcceptanceEvaluation.objects.get(execution=self.execution)
        self.assertTrue(evaluation.submission_ready)
        self.assertEqual(Submission.objects.filter(job=self.job).count(), 0)

        stat = ModelStat.objects.get(model="claude-opus-4-8", task_class="research_web")
        self.assertEqual(stat.attempts, 1)
        self.assertEqual(stat.successful_executions, 1)
        self.assertEqual(stat.credits, Decimal("89.7210"))
        self.assertEqual(stat.cost_equivalent, Decimal("0.8972"))
        self.assertEqual(stat.qa_accepted, 1)
        self.assertEqual(stat.retry_count, 0)

        self.assertTrue(first["execution_recovered"])
        self.assertFalse(first["provider_replayed"])
        self.assertTrue(second["execution_recovered"])
        self.assertTrue(second["already_recovered"])
        self.assertFalse(second["provider_replayed"])
        self.assertEqual(Artifact.objects.filter(execution=self.execution).count(), 1)
        self.assertEqual(QAResult.objects.filter(execution=self.execution).count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(event_type="genx.completed_execution_recovered").count(),
            1,
        )

    def test_expected_remote_identity_mismatch_fails_before_network_or_state_change(self):
        client = ReadOnlyCompletedGenXClient()
        gateway = GenXGateway(client=client)
        with self.assertRaisesRegex(GenXRecoveryError, "remote job identity mismatch"):
            recover_completed_genx_call(
                self.call.id,
                expected_remote_job_id="gnxsh_job_wrong",
                gateway=gateway,
            )
        self.assertEqual(client.read_calls, [])
        self.assertEqual(client.mutation_calls, [])
        self.call.refresh_from_db(); self.plan.refresh_from_db(); self.execution.refresh_from_db()
        self.assertEqual(self.call.status, "SUBMITTED")
        self.assertEqual(self.plan.status, WorkPlan.Status.FAILED)
        self.assertEqual(self.execution.status, "FAILED")

    def test_nonterminal_remote_job_blocks_recovery_without_replay(self):
        client = ReadOnlyCompletedGenXClient(status="running")
        gateway = GenXGateway(client=client)
        with self.assertRaisesRegex(GenXRecoveryError, "not terminal"):
            recover_completed_genx_call(
                self.call.id,
                expected_remote_job_id="gnxsh_job_phase1_completed",
                gateway=gateway,
            )
        self.assertEqual(client.mutation_calls, [])
        self.call.refresh_from_db(); self.plan.refresh_from_db(); self.execution.refresh_from_db()
        self.assertEqual(self.call.status, "SUBMITTED")
        self.assertEqual(self.plan.status, WorkPlan.Status.FAILED)
        self.assertEqual(self.execution.status, "FAILED")
