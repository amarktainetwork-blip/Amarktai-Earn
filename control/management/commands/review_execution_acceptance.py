import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from control.models import Artifact, AuditEvent, Execution, Job
from planning.acceptance import evaluate_execution_acceptance
from planning.models import AcceptanceContract, WorkPlan


class Command(BaseCommand):
    help = "Record an explicit human review of every semantic acceptance criterion."

    def add_arguments(self, parser):
        parser.add_argument("--execution-id", type=int, required=True)
        parser.add_argument(
            "--criterion",
            action="append",
            default=[],
            metavar="ID=PASS|FAIL|UNCERTAIN",
            help="Repeat once for every semantic criterion.",
        )
        parser.add_argument("--reviewer", required=True)
        parser.add_argument("--note", required=True)
        parser.add_argument("--format", choices=("text", "json"), default="text")

    @staticmethod
    def _criteria(values):
        result = {}
        for value in values:
            key, separator, state = str(value).partition("=")
            key, state = key.strip(), state.strip().upper()
            if not separator or not key or state not in {"PASS", "FAIL", "UNCERTAIN"}:
                raise CommandError(f"invalid --criterion {value!r}; expected ID=PASS|FAIL|UNCERTAIN")
            if key in result:
                raise CommandError(f"criterion {key!r} was supplied more than once")
            result[key] = state
        return result

    @transaction.atomic
    def handle(self, *args, **options):
        execution = Execution.objects.select_for_update().select_related("job").get(pk=options["execution_id"])
        contract = AcceptanceContract.objects.filter(job=execution.job, is_current=True).first()
        if contract is None:
            raise CommandError("the execution has no current acceptance contract")
        expected = {
            str(row["id"])
            for row in contract.criteria
            if row.get("verification") == "semantic"
        }
        supplied = self._criteria(options["criterion"])
        if set(supplied) != expected:
            missing = sorted(expected - set(supplied))
            unknown = sorted(set(supplied) - expected)
            raise CommandError(f"review must cover every semantic criterion; missing={missing}, unknown={unknown}")
        reviewer = str(options["reviewer"]).strip()
        note = str(options["note"]).strip()
        if len(reviewer) < 2 or len(note) < 10:
            raise CommandError("reviewer is required and note must contain at least 10 characters")

        artifacts = list(Artifact.objects.select_for_update().filter(execution=execution).order_by("id"))
        if not artifacts:
            raise CommandError("the execution has no persisted artifact to review")
        artifact_evidence = [
            {"id": row.id, "sha256": row.sha256, "path": row.path, "size_bytes": row.size_bytes}
            for row in artifacts
        ]
        evidence_digest = hashlib.sha256(
            json.dumps(artifact_evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        overall = "FAIL" if "FAIL" in supplied.values() else "UNCERTAIN" if "UNCERTAIN" in supplied.values() else "PASS"
        result = execution.result if isinstance(execution.result, dict) else {}
        worker_evidence = result.get("worker_evidence") if isinstance(result.get("worker_evidence"), dict) else {}
        worker_evidence = {
            **worker_evidence,
            "semantic_acceptance": {
                "status": overall,
                "criteria": supplied,
                "review_type": "EXPLICIT_OWNER_REVIEW",
                "reviewer": reviewer,
                "note": note,
                "reviewed_at": timezone.now().isoformat(),
                "contract_id": contract.id,
                "contract_source_hash": contract.source_hash,
                "artifact_evidence_sha256": evidence_digest,
                "artifacts": artifact_evidence,
            },
        }
        execution.result = {**result, "worker_evidence": worker_evidence}
        execution.save(update_fields=["result", "updated_at"])
        evaluation = evaluate_execution_acceptance(execution.id)

        plan = WorkPlan.objects.select_for_update().filter(job=execution.job).first()
        if evaluation.submission_ready:
            execution.status = "QA_PASSED"
            execution.save(update_fields=["status", "updated_at"])
            Artifact.objects.filter(execution=execution).update(accepted=True)
            if plan:
                plan.status = WorkPlan.Status.QA_PASSED
                plan.reason_codes = []
                plan.last_error_code = ""
                plan.save(update_fields=["status", "reason_codes", "last_error_code", "updated_at"])
            if execution.job.state == Job.State.FAILED:
                execution.job.state = Job.State.EXECUTING
                execution.job.save(update_fields=["state", "updated_at"])

        AuditEvent.objects.create(
            severity="INFO" if evaluation.submission_ready else "WARNING",
            event_type="job.owner_acceptance_reviewed",
            actor=reviewer[:120],
            metadata={
                "job_id": str(execution.job_id),
                "execution_id": execution.id,
                "contract_id": contract.id,
                "criteria": supplied,
                "semantic_state": evaluation.semantic_state,
                "submission_ready": evaluation.submission_ready,
                "artifact_evidence_sha256": evidence_digest,
            },
        )
        payload = {
            "execution_id": execution.id,
            "semantic_state": evaluation.semantic_state,
            "submission_ready": evaluation.submission_ready,
            "critical_failures": evaluation.critical_failures,
            "artifact_evidence_sha256": evidence_digest,
        }
        if options["format"] == "json":
            self.stdout.write(json.dumps(payload, sort_keys=True))
        else:
            self.stdout.write(self.style.SUCCESS(f"Owner acceptance review: {payload}"))
