import csv
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class QAReport:
    passed: bool
    checks: dict[str, bool]
    evidence: dict


def verify_csv(path: Path, expected_rows: int | None = None, required_columns: list[str] | None = None) -> QAReport:
    checks = {"exists": path.is_file(), "nonempty": path.is_file() and path.stat().st_size > 0}
    rows = []
    columns = []
    if checks["nonempty"]:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = reader.fieldnames or []
            rows = list(reader)
    if expected_rows is not None:
        checks["row_count"] = len(rows) == expected_rows
    if required_columns is not None:
        checks["required_columns"] = set(required_columns).issubset(columns)
    return QAReport(passed=all(checks.values()), checks=checks, evidence={"rows": len(rows), "columns": columns})
