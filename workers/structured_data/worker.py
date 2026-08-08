import csv
import json
from pathlib import Path
from workers.base import WorkRequest, WorkResult, Worker

class StructuredDataWorker(Worker):
    worker_class = "structured_data"

    def execute(self, request: WorkRequest) -> WorkResult:
        operation = request.inputs.get("operation")
        request.workspace.mkdir(parents=True, exist_ok=True)
        if operation == "json_to_csv":
            return self._json_to_csv(request)
        if operation == "csv_normalize":
            return self._csv_normalize(request)
        return WorkResult(ok=False, error=f"unsupported operation: {operation}")

    def _json_to_csv(self, request: WorkRequest) -> WorkResult:
        source = Path(request.inputs["source"])
        rows = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return WorkResult(ok=False, error="source JSON must be a list of objects")
        headers = sorted({key for row in rows for key in row.keys()})
        target = request.workspace / "output.csv"
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return WorkResult(ok=True, artifacts=[target], evidence={"rows": len(rows), "columns": headers})

    def _csv_normalize(self, request: WorkRequest) -> WorkResult:
        source = Path(request.inputs["source"])
        target = request.workspace / "output.csv"
        with source.open("r", encoding="utf-8-sig", newline="") as src:
            reader = csv.DictReader(src)
            headers = [h.strip() for h in (reader.fieldnames or [])]
            rows = []
            for raw in reader:
                rows.append({(k or "").strip(): (v or "").strip() for k, v in raw.items()})
        with target.open("w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=headers)
            writer.writeheader(); writer.writerows(rows)
        return WorkResult(ok=True, artifacts=[target], evidence={"rows": len(rows), "columns": headers})
