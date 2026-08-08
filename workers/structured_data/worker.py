import csv
import json
from pathlib import Path
from workers.base import WorkRequest, WorkResult, Worker
from workers.tabular import safe_spreadsheet_value

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
        key_mapping = {key: str(safe_spreadsheet_value(str(key).strip())) for row in rows for key in row.keys()}
        headers = sorted(set(key_mapping.values()))
        rows = [{key_mapping[key]: value for key, value in row.items()} for row in rows]
        target = request.workspace / "output.csv"
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: safe_spreadsheet_value(value) for key, value in row.items()})
        return WorkResult(ok=True, artifacts=[target], evidence={"rows": len(rows), "columns": headers, "formula_injection_neutralized": True})

    def _csv_normalize(self, request: WorkRequest) -> WorkResult:
        source = Path(request.inputs["source"])
        target = request.workspace / "output.csv"
        with source.open("r", encoding="utf-8-sig", newline="") as src:
            reader = csv.DictReader(src)
            raw_headers = [h.strip() for h in (reader.fieldnames or [])]
            header_mapping = {header: str(safe_spreadsheet_value(header)) for header in raw_headers}
            headers = [header_mapping[header] for header in raw_headers]
            rows = []
            for raw in reader:
                rows.append({header_mapping.get((k or "").strip(), str(safe_spreadsheet_value((k or "").strip()))): (v or "").strip() for k, v in raw.items()})
        with target.open("w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=headers)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: safe_spreadsheet_value(value) for key, value in row.items()})
        return WorkResult(ok=True, artifacts=[target], evidence={"rows": len(rows), "columns": headers, "formula_injection_neutralized": True})
