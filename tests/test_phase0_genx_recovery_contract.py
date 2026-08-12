from __future__ import annotations

import inspect
import os
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import redis
from django.db import models
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

import control.queueing as queueing
from control.models import (
    AuditEvent,
    GenXAccountSnapshot,
    GenXCall,
    GenXCreditValuation,
    Job,
    JobScore,
    Marketplace,
    ModelStat,
    Worker,
)
from control.services.genx_usage_evidence import GenXUsageEvidenceError, resolve_missing_genx_usage
from gateways.genx.service import GenXGateway


TIMEOUT_ENV = {
    "GENX_RESEARCH_TOOL_NEGOTIATION_MAX_MODELS": "6",
    "GENX_WORKER_TIMEOUT_SECONDS": "240",
    "SANDBOX_EXECUTION_TIMEOUT_SECONDS": "900",
    "SANDBOX_BROKER_TIMEOUT_SECONDS": "1200",
    "MEDIA_PROCESS_TIMEOUT_SECONDS": "600",
    "WORKPLAN_RQ_TIMEOUT_MARGIN_SECONDS": "120",
    "WORKPLAN_RQ_TIMEOUT_SECONDS": "1320",
}


class Phase0TimeoutHierarchyTests(SimpleTestCase):
    def test_outer_rq_timeout_exceeds_every_bounded_internal_envelope(self):
        provider_default = inspect.signature(GenXGateway.run_session).parameters["wait_timeout_seconds"].default
        self.assertEqual(provider_default, queueing._GENX_SESSION_WAIT_SECONDS)
        with patch.dict(os.environ, TIMEOUT_ENV, clear=False):
            internal = queueing.bounded_workplan_execution_envelope_seconds()
            outer = queueing.rq_job_timeout_seconds()
        self.assertGreaterEqual(internal, 6 * provider_default)
        self.assertGreater(outer, internal)
        self.assertEqual(outer, 1320)

    def test_queue_applies_outer_timeout_to_every_priority_including_background_workplans(self):
        with patch.dict(os.environ, TIMEOUT_ENV, clear=False), patch(
            "control.queueing.connection",
            return_value=redis.Redis(host="127.0.0.1", port=1),
        ):
            assigned = queueing.queue("p3")
            background = queueing.queue("p7")
        self.assertEqual(assigned._default_timeout, 1320)
        self.assertEqual(background._default_timeout, 1320)

    def test_unsafe_outer_timeout_is_rejected_instead_of_killing_valid_failover(self):
        unsafe = {**TIMEOUT_ENV, "WORKPLAN_RQ_TIMEOUT_SECONDS": "1200"}
        with patch.dict(os.environ, unsafe, clear=False):
            with self.assertRaisesRegex(RuntimeError, "bounded internal execution envelope"):
                queueing.rq_job_timeout_seconds()

    def test_runaway_outer_timeout_remains_bounded(self):
        unsafe = {**TIMEOUT_ENV, "WORKPLAN_RQ_TIMEOUT_SECONDS": "7201"}
        with patch.dict(os.environ, unsafe, clear=False):
            with self.assertRaisesRegex(RuntimeError, "between 60 and 7200"):
                queueing.rq_job_timeout_seconds()


class Phase0GenXResultRecoveryTests(TestCase):
    def setUp(self):
        market = Marketplace.objects.create(slug="phase0-recovery", display_name="Phase 0 Recovery")
        self.job = Job.objects.create(
            marketplace=market,
            external_id="phase0-paid-call",
            title="Recover completed paid GenX result",
            task_class="Research",
            reward="1000.00",
            currency="USD",
            state=Job.State.AWARDED,
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
            id="phase0-research-worker",
            worker_class="research",
            status="READY",
        )
        GenXCreditValuation.objects.create(
            version="phase0-test-usd",
            currency="USD",
            monetary_cost_per_credit=Decimal("0.0100000000"),
            source="TEST_AUTHORITATIVE_VALUATION",
            evidence={"fixture": True},
            effective_at=timezone.now() - timedelta(minutes=5),
            verified=True,
            active=True,
        )

    def test_large_completed_data_result_reconciles_billing_and_model_stats_idempotently(self):
        field = GenXCall._meta.get_field("result_url")
        self.assertIsInstance(field, models.TextField)

        call = GenXCall.objects.create(
            request_key="phase0-existing-paid-call",
            job=self.job,
            worker=self.worker,
            model="claude-opus-4-8",
            task_class="research_web",
            external_job_id="gnxsh_job_existing_completed",
            estimated_credits="0.25",
            max_allowed_credits="100",
            credits="0",
            status="SUBMITTED",
            requested_metadata={
                "transport": "session",
                "session_id": "gnxsh_session_existing",
                "message_id": "gnxsh_message_existing",
                "remote_job_id": "gnxsh_job_existing_completed",
                "billing_truth": "PENDING",
                "cost_equivalent_truth": "ESTIMATED",
            },
        )
        large_result = "data:text/plain;base64," + ("QU1BUktUQUlfUkVTRUFSQ0g=" * 2048)
        self.assertGreater(len(large_result), 200)
        payload = {
            "job_id": "gnxsh_job_existing_completed",
            "model": "claude-opus-4-8",
            "status": "completed",
            "result_url": large_result,
            "usage": {"credits": "89.721"},
        }

        gateway = GenXGateway(client=object())
        first = gateway.reconcile_remote_job_payload(call.id, payload, source="OPERATOR_EVIDENCE")
        second = gateway.reconcile_remote_job_payload(call.id, payload, source="OPERATOR_EVIDENCE")
        first.refresh_from_db()
        second.refresh_from_db()

        self.assertEqual(first.status, "COMPLETED")
        self.assertEqual(first.result_url, large_result)
        self.assertEqual(first.credits, Decimal("89.7210"))
        self.assertEqual(first.cost_equivalent, Decimal("0.8972"))
        self.assertEqual(first.requested_metadata["billing_truth"], "ACTUAL")
        self.assertEqual(first.requested_metadata["cost_equivalent_truth"], "ACTUAL")
        self.assertEqual(first.requested_metadata["remote_job_id"], "gnxsh_job_existing_completed")

        stat = ModelStat.objects.get(model="claude-opus-4-8", task_class="research_web")
        self.assertEqual(stat.attempts, 1)
        self.assertEqual(stat.successful_executions, 1)
        self.assertEqual(stat.credits, Decimal("89.7210"))
        self.assertEqual(stat.cost_equivalent, Decimal("0.8972"))
        self.assertEqual(second.credits, first.credits)
        self.assertEqual(second.cost_equivalent, first.cost_equivalent)

    def _completed_call_without_remote_usage(self, *, max_allowed_credits="100"):
        base = timezone.now() - timedelta(minutes=10)
        call = GenXCall.objects.create(
            request_key="phase0-wallet-delta-call",
            job=self.job,
            worker=self.worker,
            model="claude-opus-4-8",
            task_class="research_web",
            external_job_id="gnxsh_job_wallet_delta",
            estimated_credits="0.25",
            max_allowed_credits=max_allowed_credits,
            credits="0",
            status="COMPLETED",
            requested_metadata={
                "transport": "session",
                "session_id": "gnxsh_session_wallet_delta",
                "remote_job_id": "gnxsh_job_wallet_delta",
                "billing_truth": "UNRESOLVED",
                "cost_equivalent_truth": "ESTIMATED_USAGE",
            },
            started_at=base + timedelta(minutes=2),
            completed_at=timezone.now(),
        )
        ModelStat.objects.create(
            model=call.model,
            task_class=call.task_class,
            attempts=1,
            successful_executions=1,
        )
        before = GenXAccountSnapshot.objects.create(available_credits="98893.278", raw={})
        after = GenXAccountSnapshot.objects.create(available_credits="98803.557", raw={})
        GenXAccountSnapshot.objects.filter(pk=before.pk).update(created_at=base + timedelta(minutes=1))
        GenXAccountSnapshot.objects.filter(pk=after.pk).update(created_at=base + timedelta(minutes=5))
        before.refresh_from_db(); after.refresh_from_db()
        return call, before, after, base

    def _read_only_wallet_client(self, base):
        class ReadOnlyClient:
            def job(self, job_id):
                return {
                    "job_id": job_id,
                    "model": "claude-opus-4-8",
                    "status": "completed",
                    "created_at": (base + timedelta(minutes=2)).isoformat(),
                    "updated_at": (base + timedelta(minutes=4)).isoformat(),
                    "result_url": "data:text/plain;base64,QQ==",
                }

            def result(self, job_id):
                return {"result_url": "data:text/plain;base64,QQ=="}

            def session_messages(self, session_id):
                return {"messages": []}

        return ReadOnlyClient()

    def test_missing_remote_usage_uses_unique_bracketed_account_delta_once(self):
        call, before, after, base = self._completed_call_without_remote_usage()
        gateway = GenXGateway(client=self._read_only_wallet_client(base))
        first = resolve_missing_genx_usage(
            call.id,
            expected_remote_job_id="gnxsh_job_wallet_delta",
            gateway=gateway,
        )
        second = resolve_missing_genx_usage(
            call.id,
            expected_remote_job_id="gnxsh_job_wallet_delta",
            gateway=gateway,
        )
        first.refresh_from_db(); second.refresh_from_db()

        self.assertEqual(first.credits, Decimal("89.7210"))
        self.assertEqual(first.cost_equivalent, Decimal("0.8972"))
        self.assertEqual(first.requested_metadata["billing_truth"], "ACTUAL")
        self.assertEqual(
            first.requested_metadata["billing_evidence_method"],
            "ACCOUNT_SNAPSHOT_DELTA_UNIQUE_ATTRIBUTION",
        )
        evidence = first.requested_metadata["billing_evidence"]
        self.assertEqual(evidence["before_snapshot_id"], before.pk)
        self.assertEqual(evidence["after_snapshot_id"], after.pk)
        self.assertEqual(evidence["derived_credits"], "89.7210")
        self.assertEqual(evidence["competing_billable_calls"], 0)
        self.assertFalse(evidence["controller_credit_ceiling_breached"])
        self.assertEqual(evidence["controller_credit_ceiling_overrun"], "0")
        stat = ModelStat.objects.get(model=call.model, task_class=call.task_class)
        self.assertEqual(stat.attempts, 1)
        self.assertEqual(stat.successful_executions, 1)
        self.assertEqual(stat.credits, Decimal("89.7210"))
        self.assertEqual(stat.cost_equivalent, Decimal("0.8972"))
        self.assertEqual(second.credits, first.credits)
        self.assertEqual(
            AuditEvent.objects.filter(event_type="genx.billing_usage_evidence_resolved").count(),
            1,
        )
        self.assertEqual(
            AuditEvent.objects.filter(event_type="genx.actual_usage_exceeded_controller_ceiling").count(),
            0,
        )

    def test_proven_actual_charge_above_controller_ceiling_is_recorded_and_flagged_once(self):
        call, before, after, base = self._completed_call_without_remote_usage(max_allowed_credits="50")
        gateway = GenXGateway(client=self._read_only_wallet_client(base))

        first = resolve_missing_genx_usage(
            call.id,
            expected_remote_job_id="gnxsh_job_wallet_delta",
            gateway=gateway,
        )
        second = resolve_missing_genx_usage(
            call.id,
            expected_remote_job_id="gnxsh_job_wallet_delta",
            gateway=gateway,
        )
        first.refresh_from_db(); second.refresh_from_db()

        self.assertEqual(first.credits, Decimal("89.7210"))
        self.assertEqual(first.cost_equivalent, Decimal("0.8972"))
        self.assertEqual(first.requested_metadata["billing_truth"], "ACTUAL")
        evidence = first.requested_metadata["billing_evidence"]
        self.assertEqual(evidence["before_snapshot_id"], before.pk)
        self.assertEqual(evidence["after_snapshot_id"], after.pk)
        self.assertTrue(evidence["controller_credit_ceiling_known"])
        self.assertTrue(evidence["controller_credit_ceiling_breached"])
        self.assertEqual(evidence["call_credit_ceiling"], "50.0000")
        self.assertEqual(evidence["controller_credit_ceiling_overrun"], "39.7210")
        self.assertTrue(evidence["accounting_truth_preserved_despite_budget_breach"])
        self.assertEqual(second.credits, first.credits)
        self.assertEqual(
            AuditEvent.objects.filter(event_type="genx.billing_usage_evidence_resolved").count(),
            1,
        )
        anomalies = AuditEvent.objects.filter(event_type="genx.actual_usage_exceeded_controller_ceiling")
        self.assertEqual(anomalies.count(), 1)
        self.assertEqual(anomalies.get().metadata["actual_credits"], "89.7210")
        self.assertEqual(anomalies.get().metadata["controller_credit_ceiling"], "50.0000")
        self.assertEqual(anomalies.get().metadata["overrun_credits"], "39.7210")
        stat = ModelStat.objects.get(model=call.model, task_class=call.task_class)
        self.assertEqual(stat.credits, Decimal("89.7210"))
        self.assertEqual(stat.cost_equivalent, Decimal("0.8972"))

    def test_account_delta_recovery_refuses_overlapping_potentially_billable_call(self):
        call, _before, _after, base = self._completed_call_without_remote_usage()
        GenXCall.objects.create(
            request_key="phase0-competing-call",
            job=self.job,
            worker=self.worker,
            model="other-model",
            task_class="research_web",
            external_job_id="gnxsh_job_competing",
            estimated_credits="1",
            max_allowed_credits="100",
            credits="0",
            status="SUBMITTED",
            requested_metadata={"billing_truth": "PENDING"},
            started_at=base + timedelta(minutes=3),
        )

        class ReadOnlyClient:
            def job(self, job_id):
                return {
                    "job_id": job_id,
                    "status": "completed",
                    "created_at": (base + timedelta(minutes=2)).isoformat(),
                    "updated_at": (base + timedelta(minutes=4)).isoformat(),
                }

            def result(self, job_id):
                return {}

            def session_messages(self, session_id):
                return {"messages": []}

        with self.assertRaisesRegex(GenXUsageEvidenceError, "ambiguous"):
            resolve_missing_genx_usage(
                call.id,
                expected_remote_job_id="gnxsh_job_wallet_delta",
                gateway=GenXGateway(client=ReadOnlyClient()),
            )
        call.refresh_from_db()
        self.assertEqual(call.credits, Decimal("0.0000"))
        self.assertEqual(call.requested_metadata["billing_truth"], "UNRESOLVED")
