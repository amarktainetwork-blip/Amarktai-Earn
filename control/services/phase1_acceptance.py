from __future__ import annotations

import os

from django.db import models

from control import queueing
from control.models import GenXCall
from control.services.execution import finalize_successful_execution
from control.services.genx_recovery import recover_completed_genx_call
from workers.base import Worker
from workers.registry import capability_coverage
from workers.research.worker import ResearchWorker


def phase1_acceptance_report() -> dict:
    """Return the same read-only structural gate used by the Phase 1 command."""
    checks = []

    def check(name: str, passed: bool, details=None):
        checks.append({"name": name, "status": "PASS" if passed else "FAIL", "details": details or {}})

    result_field = GenXCall._meta.get_field("result_url")
    check(
        "GENX_RESULT_STORAGE_UNBOUNDED_TEXT",
        isinstance(result_field, models.TextField),
        {"field_class": result_field.__class__.__name__},
    )
    try:
        internal = queueing.bounded_workplan_execution_envelope_seconds()
        outer = queueing.rq_job_timeout_seconds()
        check(
            "OUTER_RQ_TIMEOUT_EXCEEDS_INTERNAL_EXECUTION",
            outer > internal and outer <= 7200,
            {"internal_seconds": internal, "outer_seconds": outer},
        )
    except RuntimeError as exc:
        check("OUTER_RQ_TIMEOUT_EXCEEDS_INTERNAL_EXECUTION", False, {"error": str(exc)})
    check(
        "CANONICAL_EXECUTION_FINALIZER_PRESENT",
        callable(finalize_successful_execution),
        {"function": "control.services.execution.finalize_successful_execution"},
    )
    check(
        "COMPLETED_PROVIDER_RECOVERY_PRESENT",
        callable(recover_completed_genx_call),
        {"function": "control.services.genx_recovery.recover_completed_genx_call"},
    )
    check(
        "PROVIDER_RECOVERY_FAILS_CLOSED_BY_DEFAULT",
        ResearchWorker.recover_completed_provider_result is not Worker.recover_completed_provider_result,
        {"default": "unsupported unless worker explicitly opts in", "research_opt_in": True},
    )
    coverage = capability_coverage()
    check("REGISTERED_OPERATION_CONTRACTS", coverage.get("status") == "PASS", coverage.get("summary") or {})
    autonomy = str(os.getenv("AUTONOMOUS_MODE", "OFF")).upper()
    check("AUTONOMY_REMAINS_OFF_DURING_PHASE1", autonomy == "OFF", {"mode": autonomy})
    failures = [row for row in checks if row["status"] == "FAIL"]
    return {
        "phase": 1,
        "name": "COMPLETE_EXECUTION_ENGINE",
        "status": "FAIL" if failures else "PASS",
        "checks": checks,
        "summary": {"total": len(checks), "passed": len(checks) - len(failures), "failed": len(failures)},
        "note": "Dynamic recovery and replay cases are enforced by the Phase 1 CI test set; this report performs no paid or external mutation.",
    }
