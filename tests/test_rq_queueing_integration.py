from __future__ import annotations

import ast
import re
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import redis
from django.test import TestCase
from rq.job import Job as RQJob

from control.models import AuditEvent, Job, Marketplace
from control.queueing import enqueue_agentgigs_webhook, rq_failure_metadata, rq_job_id
from control.services.product_factory import _queue_internal_opportunity
from planning.models import WorkPlan
from planning.services import _queue_execution, _queue_submission


ROOT = Path(__file__).resolve().parents[1]
SAFE_RQ_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class RQ210AcceptingQueue:
    """Capture enqueue calls after exercising the installed RQ Job validator."""

    def __init__(self, name: str):
        self.name = name
        self.job_ids: list[str] = []

    def enqueue(self, func, *args, job_id: str, **kwargs):
        job = RQJob.create(
            func,
            args=args,
            id=job_id,
            connection=redis.Redis(host="127.0.0.1", port=1),
            result_ttl=kwargs.get("result_ttl"),
            failure_ttl=kwargs.get("failure_ttl"),
        )
        self.job_ids.append(job.id)
        return job


class RQJobIdContractTests(TestCase):
    def setUp(self):
        self.market = Marketplace.objects.create(slug="rq-id-test", display_name="RQ ID Test")
        self.job = Job.objects.create(
            marketplace=self.market,
            external_id="rq-id-job",
            title="RQ dispatch fixture",
            task_class="Data Analysis",
            reward="10.00",
            state=Job.State.AWARDED,
        )

    def test_helper_is_deterministic_bounded_and_rq_210_accepted(self):
        self.assertRegex(version("rq"), r"^2\.10\.")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("rq>=2.10,<2.11", requirements)
        expected = "workplan-execute-2-1"
        self.assertEqual(rq_job_id("workplan", "execute", 2, 1), expected)
        self.assertEqual(rq_job_id("workplan", "execute", 2, 1), expected)
        self.assertRegex(expected, SAFE_RQ_ID)
        accepted = RQJob.create(
            "control.tasks.execute_work_plan_task",
            args=(2,),
            id=expected,
            connection=redis.Redis(host="127.0.0.1", port=1),
        )
        self.assertEqual(accepted.id, expected)
        for unsafe in ("workplan:execute:2:1", "path/2", "has space", "unicode—dash"):
            with self.assertRaises(ValueError):
                rq_job_id(unsafe)

    def test_all_canonical_enqueue_paths_generate_ids_accepted_by_installed_rq(self):
        execution_queue = RQ210AcceptingQueue("p3_assigned")
        plan = WorkPlan.objects.create(
            job=self.job,
            worker_class="structured_data",
            operation="json_to_csv",
            input_spec={"operation": "json_to_csv"},
            status=WorkPlan.Status.READY,
        )
        allowed = SimpleNamespace(allowed=True, reason_codes=[])
        with patch("control.queueing.queue", return_value=execution_queue), patch(
            "control.services.admission.decide_admission", return_value=allowed
        ):
            self.assertTrue(_queue_execution(plan))
        self.assertEqual(execution_queue.job_ids, [f"workplan-execute-{plan.id}-1"])
        plan.refresh_from_db()
        self.assertEqual(plan.status, WorkPlan.Status.QUEUED)

        plan.status = WorkPlan.Status.QA_PASSED
        plan.save(update_fields=["status", "updated_at"])
        submission_queue = RQ210AcceptingQueue("p0_revenue_protection")
        with patch("control.queueing.queue", return_value=submission_queue):
            self.assertTrue(_queue_submission(plan))
        self.assertEqual(submission_queue.job_ids, [f"workplan-submit-{plan.id}"])

        webhook_queue = RQ210AcceptingQueue("p7_background")
        with patch("control.queueing.queue", return_value=webhook_queue):
            enqueue_agentgigs_webhook(123, "job.available")
        self.assertEqual(webhook_queue.job_ids, ["agentgigs-webhook-123"])

        product_queue = RQ210AcceptingQueue("p7_background")
        product_plan = WorkPlan.objects.create(
            job=Job.objects.create(
                marketplace=self.market,
                external_id="rq-product-job",
                title="Product fixture",
                task_class="image_generation",
                reward="20.00",
                state=Job.State.AWARDED,
            ),
            worker_class="image_product",
            operation="image_generate_product_asset",
            status=WorkPlan.Status.READY,
        )
        opportunity = SimpleNamespace(
            pk=999,
            job=product_plan.job,
            job_id=product_plan.job_id,
            product=SimpleNamespace(slug="bounded-product"),
        )
        with patch("control.queueing.queue", return_value=product_queue), patch(
            "control.services.product_factory.InternalOpportunity.objects.filter"
        ):
            self.assertTrue(_queue_internal_opportunity(opportunity))
        self.assertEqual(product_queue.job_ids, [f"product-factory-execute-{product_plan.id}-1"])

        for value in [
            *execution_queue.job_ids,
            *submission_queue.job_ids,
            *webhook_queue.job_ids,
            *product_queue.job_ids,
        ]:
            self.assertRegex(value, SAFE_RQ_ID)
            self.assertNotIn(":", value)

    def test_queue_failure_audit_is_bounded_actionable_and_redacted(self):
        class FailingQueue:
            name = "p3_assigned"

            def enqueue(self, *args, **kwargs):
                raise ValueError("invalid token=do-not-record redis://user:password@example.test/0")

        plan = WorkPlan.objects.create(
            job=self.job,
            worker_class="structured_data",
            operation="json_to_csv",
            status=WorkPlan.Status.READY,
        )
        allowed = SimpleNamespace(allowed=True, reason_codes=[])
        with patch("control.queueing.queue", return_value=FailingQueue()), patch(
            "control.services.admission.decide_admission", return_value=allowed
        ):
            self.assertFalse(_queue_execution(plan))
        metadata = AuditEvent.objects.get(event_type="job.plan_queue_failed").metadata
        self.assertEqual(metadata["exception_class"], "ValueError")
        self.assertEqual(metadata["queue_name"], "p3_assigned")
        self.assertEqual(metadata["rq_job_id"], f"workplan-execute-{plan.id}-1")
        self.assertEqual(metadata["plan_id"], plan.id)
        self.assertEqual(metadata["job_id"], str(self.job.id))
        self.assertNotIn("do-not-record", metadata["error_message"])
        self.assertNotIn("user:password", metadata["error_message"])
        self.assertLessEqual(len(metadata["error_message"]), 240)

    def test_every_rq_enqueue_custom_id_uses_the_canonical_helper(self):
        offenders = []
        for path in ROOT.rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "enqueue":
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "job_id":
                        continue
                    value = keyword.value
                    canonical = (isinstance(value, ast.Name) and value.id == "rq_id") or (
                        isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Name)
                        and value.func.id == "rq_job_id"
                    )
                    if not canonical:
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [])


class RQFailureSanitizerTests(TestCase):
    def test_direct_diagnostics_are_bounded_and_safe(self):
        detail = rq_failure_metadata(
            RuntimeError("Authorization=private-value"),
            queue_name="p7_background",
            job_id=rq_job_id("agentgigs", "webhook", 1),
        )
        self.assertEqual(detail["exception_class"], "RuntimeError")
        self.assertNotIn("private-value", detail["error_message"])
