from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workers.qa.deterministic import verify_csv


@dataclass(frozen=True)
class QAOutcome:
    passed: bool
    check_type: str
    score: float
    checks: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def run_qa(profile: str, primary: Path, worker_evidence: dict[str, Any] | None = None) -> QAOutcome:
    evidence = worker_evidence if isinstance(worker_evidence, dict) else {}
    if profile == "csv":
        result = verify_csv(
            primary,
            expected_rows=evidence.get("rows"),
            required_columns=evidence.get("columns"),
        )
        return QAOutcome(
            passed=result.passed,
            check_type="deterministic_csv",
            score=1.0 if result.passed else 0.0,
            checks=list(result.checks),
            evidence=dict(result.evidence),
        )
    raise ValueError(f"unsupported QA profile: {profile}")
