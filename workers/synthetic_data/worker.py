from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

from workers.base import WorkRequest, WorkResult, Worker


class SyntheticDataError(RuntimeError):
    pass


PII_PATTERNS = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"\b(?:\+?\d[\d .()-]{7,}\d)\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
)
ALLOWED_FIELD_TYPES = {"string", "integer", "number", "boolean"}


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _contains_pii(record: dict[str, Any]) -> bool:
    text = _canonical(record)
    return any(pattern.search(text) for pattern in PII_PATTERNS)


def _validate_schema(schema: dict) -> tuple[dict[str, dict], str]:
    fields = schema.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise SyntheticDataError("schema.fields must be a non-empty object")
    clean: dict[str, dict] = {}
    for name, rule in fields.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(rule, dict):
            raise SyntheticDataError("schema fields must have named rule objects")
        field_type = str(rule.get("type") or "")
        if field_type not in ALLOWED_FIELD_TYPES:
            raise SyntheticDataError(f"unsupported schema type for {name}")
        enum = rule.get("enum")
        if enum is not None and (not isinstance(enum, list) or not enum):
            raise SyntheticDataError(f"schema enum for {name} must be a non-empty list")
        clean[name] = {"type": field_type, "required": bool(rule.get("required", True)), "enum": enum}
    label_field = str(schema.get("label_field") or "")
    if label_field and label_field not in clean:
        raise SyntheticDataError("label_field is not present in schema.fields")
    return clean, label_field


def _type_ok(value: Any, field_type: str) -> bool:
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)


def _valid_record(record: Any, fields: dict[str, dict]) -> bool:
    if not isinstance(record, dict) or set(record) - set(fields):
        return False
    for name, rule in fields.items():
        if name not in record:
            if rule["required"]:
                return False
            continue
        value = record[name]
        if not _type_ok(value, rule["type"]):
            return False
        if rule["enum"] is not None and value not in rule["enum"]:
            return False
    return True


def _generated_value(rule: dict, generator: dict, index: int, rng: random.Random):
    kind = str(generator.get("type") or "").casefold()
    if kind == "choice":
        choices = generator.get("values") or rule.get("enum")
        if not isinstance(choices, list) or not choices:
            raise SyntheticDataError("choice generator requires values")
        return choices[index % len(choices)]
    if kind == "sequence":
        start = generator.get("start", 0)
        value = start + index
        return float(value) if rule["type"] == "number" else int(value)
    if kind == "boolean_cycle":
        return bool(index % 2)
    if kind == "template":
        template = str(generator.get("template") or "")
        if not template:
            raise SyntheticDataError("template generator requires template")
        return template.replace("{index}", str(index)).replace("{random}", str(rng.randint(0, 999999)))
    raise SyntheticDataError("unsupported or missing field generator")


def _split_names(plan: dict) -> list[str]:
    splits = plan.get("splits") or {"train": 0.8, "validation": 0.1, "test": 0.1}
    if not isinstance(splits, dict) or not splits:
        raise SyntheticDataError("generation_plan.splits must be an object")
    values = []
    total = 0.0
    for name, fraction in splits.items():
        parsed = float(fraction)
        if parsed <= 0:
            raise SyntheticDataError("split fractions must be positive")
        total += parsed
        values.append((str(name), parsed))
    if abs(total - 1.0) > 0.0001:
        raise SyntheticDataError("split fractions must sum to 1")
    slots = []
    for name, fraction in values:
        slots.extend([name] * max(1, round(fraction * 1000)))
    return slots


class SyntheticDataWorker(Worker):
    worker_class = "synthetic_data"

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            inputs = request.inputs
            if inputs.get("operation") != "synthetic_dataset_generate":
                raise SyntheticDataError("unsupported synthetic-data operation")
            mode = str(inputs.get("mode") or "COMMISSIONED").upper()
            if mode not in {"COMMISSIONED", "INVENTORY"}:
                raise SyntheticDataError("synthetic mode must be COMMISSIONED or INVENTORY")
            if inputs.get("rights_confirmed") is not True or not isinstance(inputs.get("provenance"), dict) or not inputs["provenance"]:
                raise SyntheticDataError("SYNTHETIC_RIGHTS_AND_PROVENANCE_REQUIRED")
            if mode == "INVENTORY" and not (
                inputs.get("inventory_demand_evidence") and inputs.get("inventory_budget_authorized") is True
                and os.getenv("SYNTHETIC_SPECULATIVE_INVENTORY_ENABLED", "0") == "1"
            ):
                raise SyntheticDataError("SYNTHETIC_INVENTORY_NOT_EXPLICITLY_AUTHORIZED")
            schema = inputs.get("schema")
            plan = inputs.get("generation_plan")
            if not isinstance(schema, dict) or not isinstance(plan, dict):
                raise SyntheticDataError("schema and generation_plan are required")
            fields, label_field = _validate_schema(schema)
            maximum = max(1, min(int(os.getenv("SYNTHETIC_DATA_MAX_RECORDS", "10000")), 100000))
            requested = int(plan.get("record_count") or len(inputs.get("records") or []))
            if requested < 1 or requested > maximum:
                raise SyntheticDataError("requested record count exceeds bounded limit")
            estimated_cost = float(inputs.get("estimated_generation_cost") or 0)
            authorized_cost = float(inputs.get("authorized_generation_cost") or 0)
            if estimated_cost < 0 or authorized_cost < 0 or estimated_cost > authorized_cost:
                raise SyntheticDataError("SYNTHETIC_GENERATION_BUDGET_NOT_AUTHORIZED")

            supplied = inputs.get("records")
            generated: list[Any]
            if supplied is not None:
                if not isinstance(supplied, list) or len(supplied) != requested:
                    raise SyntheticDataError("supplied records must match requested record count")
                generated = supplied
            else:
                generators = plan.get("generators")
                if not isinstance(generators, dict):
                    raise SyntheticDataError("generation_plan.generators is required")
                rng = random.Random(int(plan.get("seed") or 0))
                generated = [
                    {name: _generated_value(rule, generators.get(name) or {}, index, rng) for name, rule in fields.items()}
                    for index in range(requested)
                ]

            invalid = 0
            pii_rejected = 0
            contamination_rejected = 0
            duplicates = 0
            accepted: list[dict[str, Any]] = []
            seen: set[str] = set()
            protected_fragments = [str(value).casefold() for value in inputs.get("protected_source_fragments", []) if len(str(value)) >= 80]
            for record in generated:
                if not _valid_record(record, fields):
                    invalid += 1
                    continue
                if _contains_pii(record):
                    pii_rejected += 1
                    continue
                canonical = _canonical(record)
                if any(fragment in canonical.casefold() for fragment in protected_fragments):
                    contamination_rejected += 1
                    continue
                fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
                if fingerprint in seen:
                    duplicates += 1
                    continue
                seen.add(fingerprint)
                accepted.append(record)
            if not accepted:
                raise SyntheticDataError("no records survived validation, privacy, provenance, and deduplication checks")

            slots = _split_names(plan)
            rows = []
            split_counts = Counter()
            for index, record in enumerate(accepted):
                fingerprint = hashlib.sha256(_canonical(record).encode()).hexdigest()
                split = slots[int(fingerprint[:8], 16) % len(slots)]
                rows.append({**record, "_split": split})
                split_counts[split] += 1
            class_distribution = Counter(str(row.get(label_field)) for row in accepted) if label_field else Counter()

            request.workspace.mkdir(parents=True, exist_ok=True)
            jsonl = request.workspace / "dataset.jsonl"
            csv_path = request.workspace / "dataset.csv"
            card = request.workspace / "dataset-card.md"
            manifest = request.workspace / "dataset-manifest.json"
            jsonl.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
            headers = [*fields, "_split"]
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                writer.writeheader(); writer.writerows(rows)
            metrics = {
                "requested_records": requested, "records_generated": len(generated), "accepted_records": len(accepted),
                "duplicate_records": duplicates, "invalid_records": invalid, "pii_rejected": pii_rejected,
                "contamination_rejected": contamination_rejected, "class_distribution": dict(class_distribution),
                "split_counts": dict(split_counts), "generation_cost": estimated_cost,
                "genx_credits": float(inputs.get("genx_credits") or 0),
            }
            manifest.write_text(json.dumps({"schema": schema, "generation_plan": plan, "metrics": metrics, "provenance": inputs["provenance"]}, indent=2, sort_keys=True), encoding="utf-8")
            card.write_text(
                "# Synthetic Dataset Card\n\n"
                f"Mode: {mode}\n\nRecords accepted: {len(accepted)} of {requested}\n\n"
                f"Schema: `{json.dumps(schema, sort_keys=True)}`\n\n"
                f"Class distribution: `{json.dumps(dict(class_distribution), sort_keys=True)}`\n\n"
                f"Splits: `{json.dumps(dict(split_counts), sort_keys=True)}`\n\n"
                "Privacy/provenance: PII-like records, invalid rows, exact duplicates, protected-source matches, and schema drift were rejected.\n",
                encoding="utf-8",
            )
            return WorkResult(ok=True, artifacts=[jsonl, csv_path, card, manifest], evidence={
                "operation": "synthetic_dataset_generate", "mode": mode, "schema": schema,
                "generation_plan": plan, "provenance": inputs["provenance"], "rights_confirmed": True,
                "inventory_budget_authorized": bool(inputs.get("inventory_budget_authorized")),
                "inventory_demand_evidence": inputs.get("inventory_demand_evidence") or {},
                "dataset_card_path": str(card), "manifest_path": str(manifest), **metrics,
            })
        except (OSError, TypeError, ValueError, SyntheticDataError) as exc:
            return WorkResult(ok=False, error=str(exc))
