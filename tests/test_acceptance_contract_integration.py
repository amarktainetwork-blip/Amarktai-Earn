from django.test import TestCase

from control.models import Artifact, Execution, Job, Marketplace, QAResult, Worker
from planning.acceptance import (
    AcceptanceGateError,
    acceptance_execution_payload,
    compile_acceptance_contract,
    evaluate_execution_acceptance,
    require_submission_ready,
)
from planning.models import AcceptanceContract, AcceptanceEvaluation, WorkPlan


class AcceptanceContractIntegrationTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(slug="acceptance-market", display_name="Acceptance Market")
        self.job = Job.objects.create(
            marketplace=self.market,
            external_id="acceptance-1",
            title="Produce the verified deliverable",
            task_class="documents",
            reward="25.00",
            state=Job.State.EXECUTING,
            normalized_payload={"description": "Produce a persisted deliverable."},
        )
        self.plan = WorkPlan.objects.create(
            job=self.job,
            worker_class="documents",
            operation="document_rewrite",
            input_spec={"operation": "document_rewrite"},
            status=WorkPlan.Status.EXECUTING,
        )
        self.worker = Worker.objects.create(id="acceptance-worker", worker_class="documents", status="EXECUTING")

    def execution(self, *, qa_passed=True, result=None, artifact=True):
        execution = Execution.objects.create(
            job=self.job,
            worker=self.worker,
            attempt=Execution.objects.filter(job=self.job).count() + 1,
            status="QA_PASSED" if qa_passed else "NEEDS_REPAIR",
            result=result or {},
        )
        if artifact:
            Artifact.objects.create(job=self.job, execution=execution, path="/tmp/deliverable.txt", sha256="a" * 64, size_bytes=12)
        QAResult.objects.create(job=self.job, execution=execution, check_type="deterministic", passed=qa_passed, score=1 if qa_passed else 0)
        return execution

    def test_contract_is_persisted_versioned_and_source_change_invalidates_old_contract(self):
        first = compile_acceptance_contract(self.job, self.plan)
        self.assertEqual(first.version, 1)
        self.assertEqual(first.compiled_task["grounding_hash"], first.source_hash)
        worker_payload = acceptance_execution_payload(first)
        self.assertEqual(worker_payload["source_hash"], first.source_hash)
        self.assertEqual(worker_payload["criteria"], first.criteria)
        self.job.normalized_payload = {"description": "Produce a changed deliverable."}
        self.job.save(update_fields=["normalized_payload", "updated_at"])
        second = compile_acceptance_contract(self.job, self.plan)
        first.refresh_from_db()
        self.assertEqual(second.version, 2)
        self.assertTrue(second.is_current)
        self.assertFalse(first.is_current)
        self.assertEqual(first.status, AcceptanceContract.Status.STALE)
        self.assertIn("SOURCE_INPUT_CHANGED", first.reason_codes)

    def test_deterministic_failure_cannot_be_overridden_by_semantic_pass(self):
        self.job.normalized_payload = {"acceptance_criteria": ["The result matches the requested meaning."]}
        self.job.save(update_fields=["normalized_payload", "updated_at"])
        execution = self.execution(
            qa_passed=False,
            result={"worker_evidence": {"semantic_acceptance": {"status": "PASS"}}},
        )
        evaluation = evaluate_execution_acceptance(execution.id)
        self.assertEqual(evaluation.semantic_state, AcceptanceEvaluation.SemanticState.PASS)
        self.assertFalse(evaluation.deterministic_passed)
        self.assertFalse(evaluation.submission_ready)
        self.assertIn("DETERMINISTIC_QA_FAILED", evaluation.critical_failures)

    def test_semantic_uncertainty_blocks_submission(self):
        self.job.normalized_payload = {"acceptance_criteria": ["The result answers the buyer's central question."]}
        self.job.save(update_fields=["normalized_payload", "updated_at"])
        execution = self.execution(qa_passed=True)
        evaluation = evaluate_execution_acceptance(execution.id)
        self.assertEqual(evaluation.semantic_state, AcceptanceEvaluation.SemanticState.UNCERTAIN)
        self.assertFalse(evaluation.submission_ready)
        with self.assertRaises(AcceptanceGateError):
            require_submission_ready(execution)

    def test_structured_semantic_pass_and_deterministic_qa_open_the_gate(self):
        self.job.normalized_payload = {"acceptance_criteria": ["The result answers the buyer's central question."]}
        self.job.save(update_fields=["normalized_payload", "updated_at"])
        execution = self.execution(
            qa_passed=True,
            result={"worker_evidence": {"semantic_acceptance": {"status": "PASS", "criteria": {"source-criterion-1": "PASS"}}}},
        )
        evaluation = evaluate_execution_acceptance(execution.id)
        self.assertTrue(evaluation.deterministic_passed)
        self.assertEqual(evaluation.semantic_state, AcceptanceEvaluation.SemanticState.PASS)
        self.assertTrue(evaluation.submission_ready)
        self.assertEqual(require_submission_ready(execution).id, evaluation.id)

    def test_coding_contract_requires_explicit_passing_test_evidence(self):
        self.plan.worker_class = "code_small"
        self.plan.operation = "code_change_small"
        self.plan.input_spec = {"test_command": "python -m unittest"}
        self.plan.save(update_fields=["worker_class", "operation", "input_spec", "updated_at"])
        execution = self.execution(qa_passed=True, result={"worker_evidence": {"test_exit_code": 1}})
        evaluation = evaluate_execution_acceptance(execution.id)
        self.assertFalse(evaluation.submission_ready)
        self.assertIn("CODING_TEST_EVIDENCE_FAILED", evaluation.critical_failures)

    def test_changed_input_blocks_a_previously_passing_evaluation(self):
        execution = self.execution(qa_passed=True)
        evaluation = evaluate_execution_acceptance(execution.id)
        self.assertTrue(evaluation.submission_ready)
        self.job.title = "Changed after QA"
        self.job.save(update_fields=["title", "updated_at"])
        with self.assertRaises(AcceptanceGateError) as raised:
            require_submission_ready(execution)
        self.assertIn("ACCEPTANCE_CONTRACT_STALE", raised.exception.reason_codes)
