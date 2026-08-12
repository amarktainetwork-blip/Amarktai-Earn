from unittest.mock import patch

from django.test import TestCase

from control.models import AuditEvent, GenXCall, Job, Marketplace, Submission
from planning.models import WorkPlan
from planning.retry import FailedWorkPlanRetryError, prepare_failed_work_plan_retry, retry_failed_work_plan


class FailedWorkPlanRetryIntegrationTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(slug="retry-market", display_name="Retry Market")
        self.job = Job.objects.create(
            marketplace=self.market,
            external_id="retry-1",
            title="Research retry",
            task_class="Research",
            reward="10.00",
            state=Job.State.FAILED,
        )
        self.plan = WorkPlan.objects.create(
            job=self.job,
            worker_class="research",
            operation="research_report",
            input_spec={"operation": "research_report", "query": "test"},
            status=WorkPlan.Status.FAILED,
            execution_attempts=1,
            last_error_code="GenXModelUnavailable",
            reason_codes=["PROVIDER_ROUTER_DEFECT"],
        )

    def test_prepare_reopens_same_job_and_plan_without_erasing_history(self):
        plan = prepare_failed_work_plan_retry(
            self.plan.id,
            reason="GenX cold-start router defect corrected",
        )
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, Job.State.AWARDED)
        self.assertEqual(plan.status, WorkPlan.Status.READY)
        self.assertEqual(plan.execution_attempts, 1)
        self.assertEqual(plan.last_error_code, "")
        event = AuditEvent.objects.get(event_type="job.failed_reopened")
        self.assertEqual(event.metadata["previous_error"], "GenXModelUnavailable")
        self.assertEqual(event.metadata["execution_attempts_preserved"], 1)

    def test_submission_history_blocks_replay(self):
        Submission.objects.create(job=self.job, version=1, status="FAILED")
        with self.assertRaisesRegex(FailedWorkPlanRetryError, "submission history"):
            prepare_failed_work_plan_retry(self.plan.id, reason="fixed")
        self.job.refresh_from_db(); self.plan.refresh_from_db()
        self.assertEqual(self.job.state, Job.State.FAILED)
        self.assertEqual(self.plan.status, WorkPlan.Status.FAILED)

    def test_completed_or_ambiguous_genx_mutation_blocks_blind_replay(self):
        for status in ("COMPLETED", "SUBMITTED", "UNKNOWN_REMOTE_STATE"):
            with self.subTest(status=status):
                call = GenXCall.objects.create(job=self.job, model="provider-model", status=status)
                with self.assertRaisesRegex(FailedWorkPlanRetryError, "reconcile instead of replaying"):
                    prepare_failed_work_plan_retry(self.plan.id, reason="fixed")
                call.delete()

    def test_terminal_failed_genx_call_does_not_permanently_poison_job(self):
        GenXCall.objects.create(job=self.job, model="provider-model", status="FAILED")
        plan = prepare_failed_work_plan_retry(self.plan.id, reason="provider rejection corrected")
        self.assertEqual(plan.status, WorkPlan.Status.READY)

    @patch("planning.services._queue_execution")
    def test_retry_uses_canonical_queue_function(self, queue_execution):
        def queued(plan):
            WorkPlan.objects.filter(pk=plan.pk).update(status=WorkPlan.Status.QUEUED)
            return True

        queue_execution.side_effect = queued
        plan = retry_failed_work_plan(
            self.plan.id,
            reason="GenX cold-start router defect corrected",
            enqueue=True,
        )
        queue_execution.assert_called_once()
        self.assertEqual(plan.status, WorkPlan.Status.QUEUED)


class FailedWorkPlanRetryValidationTests(TestCase):
    def test_reason_is_required(self):
        market = Marketplace.objects.create(slug="retry-validation", display_name="Retry Validation")
        job = Job.objects.create(
            marketplace=market,
            external_id="retry-validation-1",
            title="Retry validation",
            task_class="Research",
            reward="1.00",
            state=Job.State.FAILED,
        )
        plan = WorkPlan.objects.create(job=job, status=WorkPlan.Status.FAILED)
        with self.assertRaisesRegex(FailedWorkPlanRetryError, "reason is required"):
            prepare_failed_work_plan_retry(plan.id, reason="")
