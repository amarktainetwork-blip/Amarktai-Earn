from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from control.models import Artifact, AuditEvent, Execution, Job, Marketplace, QAResult
from planning.acceptance import compile_acceptance_contract
from planning.models import WorkPlan


class OwnerAcceptanceReviewCommandTests(TestCase):
    def setUp(self):
        market = Marketplace.objects.create(slug="owner-review", display_name="Owner Review")
        self.job = Job.objects.create(
            marketplace=market,
            external_id="review-1",
            title="Review recovered report",
            task_class="Research",
            reward="0",
            state=Job.State.FAILED,
            normalized_payload={"acceptance_criteria": ["Official sources only.", "Payout timing covered."]},
        )
        self.plan = WorkPlan.objects.create(
            job=self.job, worker_class="research", operation="research_report", status=WorkPlan.Status.NEEDS_REPAIR
        )
        self.execution = Execution.objects.create(job=self.job, attempt=1, status="NEEDS_REPAIR", result={})
        Artifact.objects.create(
            job=self.job, execution=self.execution, path="/tmp/report.md", sha256="a" * 64, size_bytes=128
        )
        QAResult.objects.create(
            job=self.job, execution=self.execution, check_type="research", passed=True, score=1
        )
        compile_acceptance_contract(self.job, self.plan)

    def test_complete_owner_review_can_clear_semantic_gate_with_audit_evidence(self):
        output = StringIO()
        call_command(
            "review_execution_acceptance",
            execution_id=self.execution.id,
            criterion=["source-criterion-1=PASS", "source-criterion-2=PASS"],
            reviewer="owner",
            note="I inspected the complete recovered artifact.",
            format="json",
            stdout=output,
        )
        self.execution.refresh_from_db(); self.plan.refresh_from_db(); self.job.refresh_from_db()
        self.assertEqual(self.execution.status, "QA_PASSED")
        self.assertEqual(self.plan.status, WorkPlan.Status.QA_PASSED)
        self.assertEqual(self.job.state, Job.State.EXECUTING)
        self.assertTrue(self.execution.acceptance_evaluation.submission_ready)
        self.assertTrue(self.execution.artifacts.get().accepted)
        self.assertIn("artifact_evidence_sha256", output.getvalue())
        self.assertTrue(AuditEvent.objects.filter(event_type="job.owner_acceptance_reviewed").exists())

    def test_partial_review_is_rejected_without_mutating_evidence(self):
        with self.assertRaisesMessage(CommandError, "review must cover every semantic criterion"):
            call_command(
                "review_execution_acceptance",
                execution_id=self.execution.id,
                criterion=["source-criterion-1=PASS"],
                reviewer="owner",
                note="I inspected the recovered artifact.",
            )
        self.execution.refresh_from_db()
        self.assertNotIn("semantic_acceptance", self.execution.result)
