from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workers.base import WorkRequest, WorkResult, Worker
from workers.tabular import TabularError, normalize_rows, read_rows, write_rows


class AdvancedStructuredDataWorker(Worker):
    worker_class = "advanced_structured_data"

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            operation = str(request.inputs.get("operation") or "")
            request.workspace.mkdir(parents=True, exist_ok=True)
            if operation == "tabular_merge_join":
                return self._merge(request)
            source = Path(str(request.inputs["source"]))
            rows, headers = read_rows(source)
            if operation == "tabular_convert":
                return self._write(request, rows, headers)
            if operation == "tabular_normalize":
                rows, headers = normalize_rows(rows)
                return self._write(request, rows, headers)
            if operation == "tabular_deduplicate":
                keys = [str(key) for key in request.inputs.get("keys", []) if str(key) in headers]
                keys = keys or headers
                seen = set()
                unique = []
                for row in rows:
                    identity = tuple(json.dumps(row.get(key), sort_keys=True, default=str) for key in keys)
                    if identity not in seen:
                        seen.add(identity); unique.append(row)
                return self._write(request, unique, headers, extra={"duplicates_removed": len(rows) - len(unique)})
            if operation == "tabular_column_map":
                mapping = request.inputs.get("column_mapping")
                if not isinstance(mapping, dict) or not mapping:
                    raise TabularError("column_mapping is required")
                targets = [str(mapping.get(key, key)) for key in headers]
                if len(targets) != len(set(targets)):
                    raise TabularError("column_mapping target names must be unique")
                mapped = [{str(mapping.get(key, key)): value for key, value in row.items()} for row in rows]
                mapped, mapped_headers = normalize_rows(mapped)
                return self._write(request, mapped, mapped_headers)
            if operation == "tabular_filter_sort":
                filtered = self._filter_sort(rows, headers, request.inputs)
                return self._write(request, filtered, headers)
            if operation == "tabular_schema_validate":
                return self._validate_schema(request, rows, headers)
            return WorkResult(ok=False, error=f"unsupported operation: {operation}")
        except (OSError, KeyError, TypeError, ValueError, TabularError, json.JSONDecodeError) as exc:
            return WorkResult(ok=False, error=str(exc))

    def _target(self, request: WorkRequest, default: str = "xlsx") -> Path:
        output_format = str(request.inputs.get("output_format") or default).casefold().lstrip(".")
        if output_format not in {"csv", "json", "xlsx"}:
            raise TabularError("output_format must be csv, json, or xlsx")
        return request.workspace / f"output.{output_format}"

    def _write(self, request: WorkRequest, rows: list[dict[str, Any]], headers: list[str], *, extra=None) -> WorkResult:
        target = self._target(request)
        write_rows(target, rows, headers)
        evidence = {
            "operation": request.inputs.get("operation"), "rows": len(rows), "columns": headers,
            "formula_injection_neutralized": True, "output_format": target.suffix[1:],
        }
        evidence.update(extra or {})
        return WorkResult(ok=True, artifacts=[target], evidence=evidence)

    def _merge(self, request: WorkRequest) -> WorkResult:
        sources = [Path(str(path)) for path in request.inputs.get("sources", [])]
        if len(sources) < 2:
            raise TabularError("tabular_merge_join requires at least two sources")
        datasets = [read_rows(source) for source in sources]
        join_key = str(request.inputs.get("join_key") or "").strip()
        if join_key:
            merged = datasets[0][0]
            headers = list(datasets[0][1])
            for rows, next_headers in datasets[1:]:
                lookup = {str(row.get(join_key)): row for row in rows}
                for row in merged:
                    match = lookup.get(str(row.get(join_key)), {})
                    for key, value in match.items():
                        if key != join_key:
                            target_key = key if key not in row else f"{key}_joined"
                            row[target_key] = value
                            if target_key not in headers:
                                headers.append(target_key)
        else:
            merged = [row for rows, _ in datasets for row in rows]
            merged, headers = normalize_rows(merged)
        return self._write(request, merged, headers, extra={"source_count": len(sources), "join_key": join_key})

    def _filter_sort(self, rows: list[dict[str, Any]], headers: list[str], inputs: dict) -> list[dict[str, Any]]:
        equals = inputs.get("filter_equals") or {}
        if not isinstance(equals, dict):
            raise TabularError("filter_equals must be an object")
        filtered = [row for row in rows if all(str(row.get(str(key), "")) == str(value) for key, value in equals.items())]
        sort_by = str(inputs.get("sort_by") or "").strip()
        if sort_by:
            if sort_by not in headers:
                raise TabularError("sort_by column not found")
            values = [row.get(sort_by) for row in filtered if row.get(sort_by) not in (None, "")]
            numeric = True
            try: [float(value) for value in values]
            except (TypeError, ValueError): numeric = False
            key = (lambda row: (row.get(sort_by) in (None, ""), float(row.get(sort_by) or 0))) if numeric else (lambda row: (row.get(sort_by) in (None, ""), str(row.get(sort_by, "")).casefold()))
            filtered.sort(key=key, reverse=bool(inputs.get("descending")))
        return filtered

    def _validate_schema(self, request: WorkRequest, rows: list[dict[str, Any]], headers: list[str]) -> WorkResult:
        schema = request.inputs.get("schema")
        if not isinstance(schema, dict) or not schema:
            raise TabularError("schema is required")
        allowed = {"string": str, "number": (int, float), "integer": int, "boolean": bool}
        errors = []
        for index, row in enumerate(rows, start=2):
            for field, rule in schema.items():
                descriptor = rule if isinstance(rule, dict) else {"type": rule}
                value = row.get(str(field))
                if descriptor.get("required") and value in (None, ""):
                    errors.append({"row": index, "field": field, "code": "REQUIRED"})
                expected = allowed.get(str(descriptor.get("type") or "").casefold())
                if expected and value not in (None, "") and not isinstance(value, expected):
                    errors.append({"row": index, "field": field, "code": "TYPE"})
        report = request.workspace / "schema-validation.json"
        payload = {"valid": not errors, "rows": len(rows), "columns": headers, "errors": errors[:1000]}
        report.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        return WorkResult(ok=True, artifacts=[report], evidence={"operation": "tabular_schema_validate", "schema_valid": not errors, "error_count": len(errors), "output_format": "json"})
