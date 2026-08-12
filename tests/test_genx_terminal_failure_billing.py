from decimal import Decimal

from django.test import TestCase

from control.models import AuditEvent, GenXCall, Job, JobScore, Marketplace, ModelStat, Worker
from gateways.genx.contracts import effective_reserved_credits
from gateways.genx.service import GenXGateway


class GenXTerminalFailureBillingTests(TestCase):
    def setUp(self):
        market = Marketplace.objects.create(
            slug="terminal-genx-billing",
            display_name="Terminal GenX Billing",
        )
        self.job = Job.objects.create(
            marketplace=market,
            external_id="terminal-genx-billing-job",
            title="Terminal GenX billing proof",
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
            id="terminal-genx-worker",
            worker_class="research",
            status="READY",
        )

    def _call(self, *, request_key: str, remote_job_id: str) -> GenXCall:
        return GenXCall.objects.create(
            request_key=request_key,
            job=self.job,
            worker_id="terminal-genx-worker",
            model="provider-model",
            task_class="research_web",
            external_job_id=remote_job_id,
            estimated_credits="0.25",
            max_allowed_credits="1",
            status="SUBMITTED",
            requested_metadata={
                "remote_job_id": remote_job_id,
                "billing_truth": "PENDING",
                "cost_equivalent_truth": "ESTIMATED_USAGE",
                "estimated_cost_equivalent": "0.0025",
                "accounting_currency": "USD",
            },
        )

    def test_authoritative_failed_without_usage_is_nonbillable_and_releases_reservation(self):
        call = self._call(
            request_key="failed-no-usage",
            remote_job_id="job-failed",
        )

        GenXGateway().reconcile_remote_job_payload(
            call.id,
            {
                "job_id": "job-failed",
                "status": "failed",
                "error": "Anthropic provider error (524)",
            },
            source="POLL",
        )

        call.refresh_from_db()
        self.assertEqual(call.status, "FAILED")
        self.assertEqual(call.credits, Decimal("0"))
        self.assertEqual(call.cost_equivalent, Decimal("0"))
        self.assertEqual(call.error_code, "Anthropic provider error (524)")
        self.assertEqual(call.requested_metadata["billing_truth"], "NOT_APPLICABLE")
        self.assertEqual(call.requested_metadata["cost_equivalent_truth"], "NOT_APPLICABLE")
        self.assertIsNone(call.requested_metadata["estimated_cost_equivalent"])

        reserved = effective_reserved_credits(
            GenXCall.objects.filter(pk=call.pk).values_list(
                "credits",
                "estimated_credits",
                "status",
                "requested_metadata",
            )
        )
        self.assertEqual(reserved, Decimal("0"))
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type="genx.failed_job_nonbillable",
                metadata__call_id=str(call.id),
            ).exists()
        )
        stat = ModelStat.objects.get(model="provider-model", task_class="research_web")
        self.assertEqual(stat.failures, 1)
        self.assertEqual(stat.provider_failures, 1)

    def test_completed_without_usage_remains_unresolved_and_reserved(self):
        call = self._call(
            request_key="completed-no-usage",
            remote_job_id="job-completed",
        )

        GenXGateway().reconcile_remote_job_payload(
            call.id,
            {
                "job_id": "job-completed",
                "status": "completed",
                "result_url": "data/plain;base64,T0s=",
            },
            source="POLL",
        )

        call.refresh_from_db()
        self.assertEqual(call.status, "COMPLETED")
        self.assertEqual(call.credits, Decimal("0"))
        self.assertIsNone(call.cost_equivalent)
        self.assertEqual(call.requested_metadata["billing_truth"], "UNRESOLVED")
        self.assertNotEqual(call.requested_metadata["cost_equivalent_truth"], "NOT_APPLICABLE")

        reserved = effective_reserved_credits(
            GenXCall.objects.filter(pk=call.pk).values_list(
                "credits",
                "estimated_credits",
                "status",
                "requested_metadata",
            )
        )
        self.assertEqual(reserved, Decimal("0.25"))