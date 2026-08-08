from __future__ import annotations

from decimal import Decimal

from control.models import SyntheticDatasetRun


def persist_synthetic_dataset_run(*, job, execution, evidence: dict, qa_passed: bool) -> SyntheticDatasetRun:
    generated = max(0, int(evidence.get("records_generated") or 0))
    accepted = max(0, int(evidence.get("accepted_records") or 0))
    cost = Decimal(str(evidence.get("generation_cost") or 0))
    rejection_rate = Decimal(generated - accepted) / Decimal(generated) if generated else Decimal("0")
    cost_per_record = cost / Decimal(accepted) if accepted else Decimal("0")
    artifacts = [
        {"path": row.path, "sha256": row.sha256, "size_bytes": row.size_bytes, "mime_type": row.mime_type}
        for row in execution.artifacts.order_by("id")
    ]
    return SyntheticDatasetRun.objects.update_or_create(
        execution=execution,
        defaults={
            "job": job,
            "mode": str(evidence.get("mode") or "COMMISSIONED"),
            "status": SyntheticDatasetRun.Status.COMPLETED if qa_passed else SyntheticDatasetRun.Status.REJECTED,
            "schema": evidence.get("schema") or {},
            "generation_plan": evidence.get("generation_plan") or {},
            "provenance": evidence.get("provenance") or {},
            "rights_confirmed": evidence.get("rights_confirmed") is True,
            "demand_evidence": evidence.get("inventory_demand_evidence") or {},
            "budget_authorized": evidence.get("inventory_budget_authorized") is True,
            "requested_records": max(0, int(evidence.get("requested_records") or 0)),
            "records_generated": generated,
            "accepted_records": accepted,
            "duplicate_records": max(0, int(evidence.get("duplicate_records") or 0)),
            "invalid_records": max(0, int(evidence.get("invalid_records") or 0)),
            "class_distribution": evidence.get("class_distribution") or {},
            "split_counts": evidence.get("split_counts") or {},
            "generation_cost": cost,
            "genx_credits": Decimal(str(evidence.get("genx_credits") or 0)),
            "cost_per_accepted_record": cost_per_record,
            "qa_rejection_rate": rejection_rate,
            "artifact_manifest": {"artifacts": artifacts},
            "reason_codes": [] if qa_passed else ["SYNTHETIC_DATASET_QA_FAILED"],
        },
    )[0]
