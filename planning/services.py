from __future__ import annotations

import hashlib
import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from control.models import AuditEvent, Execution, GenXCall, Job, QAResult, Submission
from planning.asset_policy import AssetPolicyError, inspect_asset, safe_asset_name, validate_role
from planning.models import JobAsset, JobAssetManifest, RepositorySnapshot, WorkPlan, WorkPlanStep, WorkPlanStepDependency
from workers.registry import WorkerRegistryError, operation_spec
from control.services.workload_policy import evaluate_job


class PlanningError(RuntimeError):
    pass


PLANNER_VERSION = "deterministic-v2"


def _inside(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    base = root.resolve()
    return resolved == base or base in resolved.parents


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rebuild_asset_manifest(job: Job) -> JobAssetManifest:
    assets = list(JobAsset.objects.filter(job=job).order_by("created_at", "id"))
    verified = [row for row in assets if row.status == JobAsset.Status.VERIFIED and row.duplicate_of_id is None]
    reasons = []
    max_files = max(1, int(os.getenv("JOB_ASSET_MAX_FILES", "12")))
    max_total = max(1, int(os.getenv("JOB_ASSET_MAX_TOTAL_BYTES", str(250 * 1024 * 1024))))
    total = sum(row.size_bytes for row in verified)
    if len(verified) > max_files:
        reasons.append("ASSET_FILE_COUNT_LIMIT")
    if total > max_total:
        reasons.append("ASSET_TOTAL_BYTES_LIMIT")
    if any(row.status == JobAsset.Status.BLOCKED for row in assets):
        reasons.append("ASSET_BLOCKED")
        for row in assets:
            if row.status == JobAsset.Status.BLOCKED and isinstance(row.metadata, dict):
                reasons.extend(str(code) for code in row.metadata.get("reason_codes", []) if code)
    roles: dict[str, list[int]] = {}
    for row in verified:
        roles.setdefault(row.semantic_role, []).append(row.id)
    material = [
        {"id": row.id, "role": row.semantic_role, "sha256": row.sha256, "bytes": row.size_bytes, "mime": row.detected_mime_type}
        for row in verified
    ]
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest() if material else ""
    status = JobAssetManifest.Status.VERIFIED if verified and not reasons else JobAssetManifest.Status.BLOCKED
    manifest, _ = JobAssetManifest.objects.update_or_create(
        job=job,
        defaults={
            "status": status,
            "file_count": len(verified),
            "total_bytes": total,
            "manifest_sha256": digest,
            "roles": roles,
            "reason_codes": reasons or ([] if verified else ["NO_VERIFIED_ASSETS"]),
            "verified_at": timezone.now() if status == JobAssetManifest.Status.VERIFIED else None,
        },
    )
    return manifest


def stage_local_job_asset(*, job_id, path: str, source: str = "upload", external_id: str = "", semantic_role: str = "source", declared_mime_type: str = "") -> JobAsset:
    job = Job.objects.get(pk=job_id)
    candidate = Path(path).resolve()
    upload_root = Path(os.getenv("AMARKTAI_UPLOAD_ROOT", "/var/lib/amarktai-earn/uploads")).resolve()
    job_root = Path(os.getenv("AMARKTAI_JOB_ROOT", "/var/lib/amarktai-earn/jobs")).resolve()
    if not (_inside(candidate, upload_root) or _inside(candidate, job_root)):
        raise PlanningError("job asset is outside approved upload/job storage")
    if candidate.is_symlink():
        raise PlanningError("job asset symlinks are not permitted")
    if not candidate.is_file():
        raise PlanningError("job asset file does not exist")
    role = validate_role(semantic_role)
    name = safe_asset_name(candidate.name)
    size = candidate.stat().st_size
    maximum = max(1, int(os.getenv("JOB_ASSET_MAX_FILE_BYTES", str(100 * 1024 * 1024))))
    if size > maximum:
        raise PlanningError("job asset exceeds per-file size limit")
    try:
        inspection = inspect_asset(candidate)
    except AssetPolicyError as exc:
        raise PlanningError(exc.code) from exc
    digest = _sha256(candidate)
    duplicate = JobAsset.objects.filter(job=job, sha256=digest, status=JobAsset.Status.VERIFIED).exclude(path=str(candidate)).first()
    defaults = {
        "semantic_role": role,
        "source": source[:40],
        "name": name,
        "path": str(candidate),
        "sha256": digest,
        "size_bytes": size,
        "mime_type": inspection.detected_mime_type,
        "declared_mime_type": declared_mime_type[:120],
        "detected_mime_type": inspection.detected_mime_type,
        "archive_inspected": inspection.archive_inspected,
        "duplicate_of": duplicate,
        "status": JobAsset.Status.BLOCKED if duplicate else JobAsset.Status.VERIFIED,
        "verified_at": None if duplicate else timezone.now(),
        "metadata": {"reason_codes": ["ASSET_DUPLICATE"]} if duplicate else {},
    }
    if external_id:
        asset, _ = JobAsset.objects.update_or_create(job=job, external_id=external_id, defaults=defaults)
    else:
        asset = JobAsset.objects.filter(job=job, path=str(candidate)).order_by("-created_at").first()
        if asset:
            for key, value in defaults.items():
                setattr(asset, key, value)
            asset.save()
        else:
            asset = JobAsset.objects.create(job=job, external_id="", **defaults)
    manifest = rebuild_asset_manifest(job)
    WorkPlan.objects.filter(job=job, status__in=[WorkPlan.Status.FAILED, WorkPlan.Status.BLOCKED]).update(
        status=WorkPlan.Status.BLOCKED,
        reason_codes=["INPUT_ASSET_CHANGED_REPLAN_REQUIRED"],
        last_error_code="",
    )
    AuditEvent.objects.create(
        event_type="job.asset_staged",
        actor="asset-stager",
        metadata={"job_id": str(job.id), "asset_id": asset.id, "source": source, "role": role, "sha256": asset.sha256, "manifest_id": manifest.id},
    )
    return asset


def _instruction_parts(job: Job) -> list[str]:
    raw = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    fields = [job.title]
    for key in ("description", "requirements", "instructions", "task", "deliverables"):
        value = raw.get(key)
        if isinstance(value, str):
            fields.append(value)
        elif isinstance(value, list):
            fields.extend(str(item) for item in value if isinstance(item, (str, int, float)))
    return [str(value).strip() for value in fields if str(value).strip()]


def _instruction_text(job: Job) -> str:
    return " ".join(_instruction_parts(job)).casefold()


def _target_language(job: Job) -> str:
    raw = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    for key in ("target_language", "targetLanguage", "language", "locale"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    text = " ".join(_instruction_parts(job))
    common = (
        "English", "Spanish", "French", "German", "Italian", "Portuguese", "Russian",
        "Dutch", "Afrikaans", "Arabic", "Chinese", "Japanese", "Korean", "Hindi",
    )
    import re
    for language in common:
        if re.search(rf"\b(?:into|to|in)\s+{re.escape(language)}\b", text, re.IGNORECASE):
            return language
    return ""


def _time_seconds(value: str) -> float:
    parts = [int(item) for item in value.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        hours = 0
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError("invalid media timestamp")
    if minutes > 59 or seconds > 59:
        raise ValueError("invalid media timestamp")
    return float(hours * 3600 + minutes * 60 + seconds)


def _infer_operation(job: Job, asset: JobAsset | None) -> tuple[str, dict, list[str]]:
    text = _instruction_text(job)
    raw_instructions = "\n".join(_instruction_parts(job))
    raw_payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    suffix = Path(asset.path).suffix.casefold() if asset is not None else ""
    data_suffixes = {".json", ".csv", ".xlsx"}
    document_suffixes = {".pdf", ".docx", ".txt", ".md"}
    image_suffixes = {".jpg", ".jpeg", ".png", ".webp"}
    media_suffixes = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4", ".mov", ".webm"}

    explicit = str(raw_payload.get("operation") or "").strip()
    if explicit:
        try:
            spec = operation_spec(explicit)
        except WorkerRegistryError:
            return "", {}, ["EXPLICIT_OPERATION_NOT_REGISTERED"]
        raw_inputs = raw_payload.get("inputs") or {}
        if not isinstance(raw_inputs, dict):
            return "", {}, ["EXPLICIT_INPUT_SPEC_INVALID"]
        inputs = dict(raw_inputs)
        for key, value in raw_payload.items():
            if key not in {"operation", "inputs", "workflow_steps", "asset_roles"}:
                inputs.setdefault(key, value)
        for unsafe_path in ("source", "sources", "repository_path"):
            inputs.pop(unsafe_path, None)
        inputs["operation"] = explicit
        if asset is not None:
            if spec.input_suffixes and suffix not in spec.input_suffixes:
                return "", {}, ["INPUT_TYPE_NOT_SUPPORTED"]
            inputs["source"] = asset.path
            inputs["asset_id"] = asset.id
        embedded_input = any(inputs.get(key) for key in ("content", "slides", "sections", "brief", "requirements", "url"))
        snapshot = None
        if explicit in {"defensive_code_review", "technical_documentation"}:
            snapshot = RepositorySnapshot.objects.filter(job=job, status=RepositorySnapshot.Status.VERIFIED).first()
        if asset is None and spec.input_suffixes and not embedded_input and not (explicit == "technical_documentation" and snapshot):
            return "", {}, ["INPUT_ASSET_NOT_STAGED"]
        if explicit == "public_web_extract":
            public_reasons = []
            if os.getenv("PUBLIC_WEB_DATA_ENABLED", "0") != "1":
                public_reasons.append("PUBLIC_WEB_DATA_DISABLED")
            if inputs.get("authorization_confirmed") is not True or inputs.get("terms_permit") is not True:
                public_reasons.append("PUBLIC_WEB_POLICY_PROOF_REQUIRED")
            if not str(inputs.get("purpose") or "").strip():
                public_reasons.append("PUBLIC_WEB_PURPOSE_REQUIRED")
            if public_reasons:
                return "", {}, public_reasons
        if explicit == "defensive_code_review":
            defensive_reasons = []
            if inputs.get("authorization_confirmed") is not True:
                defensive_reasons.append("DEFENSIVE_REVIEW_AUTHORIZATION_REQUIRED")
            if not str(inputs.get("scope") or "").strip():
                defensive_reasons.append("DEFENSIVE_REVIEW_SCOPE_REQUIRED")
            if defensive_reasons:
                return "", {}, defensive_reasons
        if explicit == "synthetic_dataset_generate":
            synthetic_reasons = []
            if inputs.get("rights_confirmed") is not True or not isinstance(inputs.get("provenance"), dict) or not inputs.get("provenance"):
                synthetic_reasons.append("SYNTHETIC_RIGHTS_AND_PROVENANCE_REQUIRED")
            if not isinstance(inputs.get("schema"), dict) or not isinstance(inputs.get("generation_plan"), dict):
                synthetic_reasons.append("SYNTHETIC_SCHEMA_AND_PLAN_REQUIRED")
            if str(inputs.get("mode") or "COMMISSIONED").upper() == "INVENTORY" and not (
                inputs.get("inventory_demand_evidence") and inputs.get("inventory_budget_authorized") is True
                and os.getenv("SYNTHETIC_SPECULATIVE_INVENTORY_ENABLED", "0") == "1"
            ):
                synthetic_reasons.append("SYNTHETIC_INVENTORY_NOT_EXPLICITLY_AUTHORIZED")
            if synthetic_reasons:
                return "", {}, synthetic_reasons
        if explicit == "ai_safety_evaluate":
            return "", {}, ["SAFETY_AUTHORIZATION_SERVICE_REQUIRED"]
        if explicit in {"defensive_code_review", "technical_documentation"}:
            if snapshot and snapshot.path and Path(snapshot.path).is_dir():
                inputs["repository_path"] = snapshot.path
                inputs["repository_snapshot_id"] = snapshot.id
            elif explicit == "defensive_code_review":
                return "", {}, ["REPOSITORY_NOT_STAGED"]
        return explicit, inputs, []

    if asset is None and any(term in text for term in ("research", "investigate", "web research", "find sources")):
        return "research_report", {"operation": "research_report", "query": job.title, "requirements": raw_instructions}, []
    if asset is None:
        return "", {}, ["INPUT_ASSET_NOT_STAGED"]

    if suffix == ".json" and "csv" in text and any(term in text for term in ("convert", "conversion", "to csv")):
        return "json_to_csv", {"operation": "json_to_csv", "source": asset.path, "asset_id": asset.id}, []
    if suffix == ".csv" and any(term in text for term in ("normalize", "normalise", "clean csv", "clean the csv", "trim whitespace", "standardize", "standardise")):
        return "csv_normalize", {"operation": "csv_normalize", "source": asset.path, "asset_id": asset.id}, []
    if suffix in data_suffixes and any(term in text for term in ("deduplicate", "de-duplicate", "remove duplicates")):
        return "tabular_deduplicate", {"operation": "tabular_deduplicate", "source": asset.path, "asset_id": asset.id, "output_format": suffix.lstrip(".")}, []
    if suffix in data_suffixes and any(term in text for term in ("spreadsheet report", "professional spreadsheet", "xlsx report")):
        return "spreadsheet_report", {"operation": "spreadsheet_report", "source": asset.path, "asset_id": asset.id}, []
    if suffix in data_suffixes and any(term in text for term in ("descriptive analysis", "data analysis report", "data quality report")):
        return "data_analysis_report", {"operation": "data_analysis_report", "source": asset.path, "asset_id": asset.id}, []
    if suffix in {".html", ".htm"} and any(term in text for term in ("seo audit", "content audit", "page audit")):
        return "seo_content_audit", {"operation": "seo_content_audit", "source": asset.path, "asset_id": asset.id}, []

    if suffix in image_suffixes:
        match = re.search(r"\bresize(?: the)? image to (\d{1,5})\s*x\s*(\d{1,5})\b", text)
        if match:
            return "image_resize", {"operation": "image_resize", "source": asset.path, "asset_id": asset.id, "width": int(match.group(1)), "height": int(match.group(2)), "output_format": "JPEG" if suffix in {".jpg", ".jpeg"} else suffix[1:].upper()}, []
        match = re.search(r"\b(?:create|make)(?: a)? (?:bounded )?thumbnail(?: of)? (\d{1,5})\s*x\s*(\d{1,5})\b", text)
        if match:
            return "image_thumbnail", {"operation": "image_thumbnail", "source": asset.path, "asset_id": asset.id, "width": int(match.group(1)), "height": int(match.group(2)), "output_format": "JPEG" if suffix in {".jpg", ".jpeg"} else suffix[1:].upper()}, []
        match = re.search(r"\bcenter(?:ed)? crop(?: the)? image to (\d{1,5})\s*x\s*(\d{1,5})\b", text)
        if match:
            return "image_center_crop", {"operation": "image_center_crop", "source": asset.path, "asset_id": asset.id, "width": int(match.group(1)), "height": int(match.group(2)), "output_format": "JPEG" if suffix in {".jpg", ".jpeg"} else suffix[1:].upper()}, []
        match = re.search(r"\bconvert(?: the)? (?:image|png|jpe?g|webp)?\s*(?:to|as) (jpe?g|png|webp)(?: (?:at )?quality (\d{2}))?\b", text)
        if match:
            target = "JPEG" if match.group(1) in {"jpg", "jpeg"} else match.group(1).upper()
            spec = {"operation": "image_convert", "source": asset.path, "asset_id": asset.id, "output_format": target}
            if match.group(2):
                spec["quality"] = int(match.group(2))
            return "image_convert", spec, []
        match = re.search(r"\b(?:compress|optimi[sz]e)(?: the)? image (?:to|as) (jpe?g|webp) quality (\d{2})\b", text)
        if match:
            target = "JPEG" if match.group(1) in {"jpg", "jpeg"} else "WEBP"
            return "image_compress", {"operation": "image_compress", "source": asset.path, "asset_id": asset.id, "output_format": target, "quality": int(match.group(2))}, []

    if suffix in document_suffixes:
        if any(term in text for term in ("translate", "translation", "localize", "localise", "localization", "localisation")):
            language = _target_language(job)
            if not language:
                return "", {}, ["TARGET_LANGUAGE_NOT_EXPLICIT"]
            return "translate_document", {"operation": "translate_document", "source": asset.path, "asset_id": asset.id, "target_language": language}, []
        if any(term in text for term in ("summarize", "summarise", "summary of", "create a summary")):
            return "document_summarize", {"operation": "document_summarize", "source": asset.path, "asset_id": asset.id}, []
        if any(term in text for term in ("rewrite", "polish", "improve wording", "edit this document", "edit the document")):
            return "document_rewrite", {"operation": "document_rewrite", "source": asset.path, "asset_id": asset.id, "instructions": raw_instructions}, []
        if any(term in text for term in ("extract text", "extract the text", "convert to text", "plain text")):
            return "document_extract_text", {"operation": "document_extract_text", "source": asset.path, "asset_id": asset.id}, []

    if suffix in media_suffixes and any(term in text for term in ("transcribe", "transcription", "speech to text", "speech-to-text")):
        return "transcribe_media", {"operation": "transcribe_media", "source": asset.path, "asset_id": asset.id}, []

    if suffix in media_suffixes:
        match = re.search(r"\btrim (?:the )?(?:audio|video|media) from (\d{1,2}:\d{2}(?::\d{2})?) to (\d{1,2}:\d{2}(?::\d{2})?)\b", text)
        if match:
            try:
                start, end = _time_seconds(match.group(1)), _time_seconds(match.group(2))
            except ValueError:
                return "", {}, ["MEDIA_TIME_RANGE_INVALID"]
            output = "mp4" if suffix in {".mp4", ".mov"} else "webm" if suffix == ".webm" else "mp3" if suffix != ".wav" else "wav"
            return "media_trim", {"operation": "media_trim", "source": asset.path, "asset_id": asset.id, "start_seconds": start, "end_seconds": end, "output_format": output}, []
        match = re.search(r"\bextract (?:the )?audio (?:track )?(?:to|as) (mp3|wav)\b", text)
        if match and suffix in {".mp4", ".mov", ".webm"}:
            return "media_extract_audio", {"operation": "media_extract_audio", "source": asset.path, "asset_id": asset.id, "output_format": match.group(1)}, []
        match = re.search(r"\b(?:transcode|convert)(?: the)? (?:audio|video|media) (?:to|as) (mp4|webm|mp3|wav)\b", text)
        if match:
            if suffix in {".mp3", ".wav", ".m4a", ".ogg", ".flac"} and match.group(1) in {"mp4", "webm"}:
                return "", {}, ["MEDIA_OUTPUT_INCOMPATIBLE"]
            return "media_transcode", {"operation": "media_transcode", "source": asset.path, "asset_id": asset.id, "output_format": match.group(1)}, []

    if suffix not in data_suffixes | document_suffixes | image_suffixes | media_suffixes | {".html", ".htm"}:
        return "", {}, ["SOURCE_TYPE_NOT_SUPPORTED_BY_REGISTERED_WORKER"]
    return "", {}, ["TRANSFORMATION_NOT_UNAMBIGUOUS"]


def _topological_steps(raw_steps: list[dict]) -> tuple[list[dict], list[str]]:
    reasons = []
    by_key = {}
    for raw in raw_steps:
        if not isinstance(raw, dict):
            reasons.append("COMPOSITE_STEP_INVALID")
            continue
        key = str(raw.get("key") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", key) or key in by_key:
            reasons.append("COMPOSITE_STEP_KEY_INVALID")
            continue
        by_key[key] = raw
    dependencies = {}
    for key, raw in by_key.items():
        deps = raw.get("depends_on") or []
        if not isinstance(deps, list) or any(str(dep) not in by_key or str(dep) == key for dep in deps):
            reasons.append("COMPOSITE_DEPENDENCY_INVALID")
            deps = []
        dependencies[key] = [str(dep) for dep in deps]
    ordered = []
    remaining = dict(dependencies)
    while remaining:
        ready = sorted(key for key, deps in remaining.items() if all(dep in {item["key"] for item in ordered} for dep in deps))
        if not ready:
            reasons.append("COMPOSITE_DEPENDENCY_CYCLE")
            break
        for key in ready:
            ordered.append({**by_key[key], "key": key, "depends_on": dependencies[key]})
            remaining.pop(key)
    return ordered, list(dict.fromkeys(reasons))


def _build_composite_plan(plan: WorkPlan, job: Job, assets: list[JobAsset], raw_steps: list[dict]) -> list[str]:
    maximum = max(1, min(int(os.getenv("WORKPLAN_MAX_COMPOSITE_STEPS", "8")), 20))
    if len(raw_steps) < 2:
        return ["COMPOSITE_REQUIRES_MULTIPLE_STEPS"]
    if len(raw_steps) > maximum:
        return ["COMPOSITE_STEP_LIMIT"]
    ordered, reasons = _topological_steps(raw_steps)
    if reasons:
        return reasons
    by_role: dict[str, list[JobAsset]] = {}
    for asset in assets:
        by_role.setdefault(asset.semantic_role, []).append(asset)
    WorkPlanStep.objects.filter(plan=plan).delete()
    created: dict[str, WorkPlanStep] = {}
    for sequence, raw in enumerate(ordered, start=1):
        operation = str(raw.get("operation") or "").strip()
        try:
            spec = operation_spec(operation)
        except WorkerRegistryError:
            reasons.append(f"STEP_OPERATION_NOT_REGISTERED:{raw['key']}")
            continue
        roles = raw.get("input_asset_roles") or []
        if not isinstance(roles, list):
            reasons.append(f"STEP_ASSET_ROLES_INVALID:{raw['key']}")
            continue
        selected_assets = []
        for role in roles:
            try:
                normalized = validate_role(str(role))
            except AssetPolicyError:
                reasons.append(f"STEP_ASSET_ROLE_NOT_APPROVED:{raw['key']}")
                continue
            selected_assets.extend(by_role.get(normalized, []))
            if normalized not in by_role:
                reasons.append(f"STEP_REQUIRED_ASSET_ROLE_MISSING:{raw['key']}:{normalized}")
        if not roles and not raw["depends_on"] and spec.input_suffixes:
            reasons.append(f"STEP_INPUT_NOT_DECLARED:{raw['key']}")
        raw_input_spec = raw.get("input_spec") or {}
        if not isinstance(raw_input_spec, dict):
            reasons.append(f"STEP_INPUT_SPEC_INVALID:{raw['key']}")
            continue
        try:
            max_repairs = max(
                0,
                min(
                    int(raw.get("max_repair_attempts", os.getenv("MAX_DETERMINISTIC_REPAIR_ATTEMPTS", "1"))),
                    3,
                ),
            )
            estimated_cost = max(Decimal("0"), Decimal(str(raw.get("estimated_cost") or "0")))
        except (InvalidOperation, TypeError, ValueError):
            reasons.append(f"STEP_COST_OR_REPAIR_BOUND_INVALID:{raw['key']}")
            continue
        input_spec = dict(raw_input_spec)
        input_spec["operation"] = operation
        input_spec["input_asset_roles"] = [str(role) for role in roles]
        step = WorkPlanStep.objects.create(
            plan=plan,
            key=raw["key"],
            sequence=sequence,
            operation=operation,
            worker_class=spec.worker_class,
            input_spec=input_spec,
            status=WorkPlanStep.Status.BLOCKED,
            max_repair_attempts=max_repairs,
            estimated_cost=estimated_cost,
            reason_codes=["WAITING_FOR_UPSTREAM_QA"] if raw["depends_on"] else [],
        )
        step.input_assets.add(*selected_assets)
        created[raw["key"]] = step
    if reasons:
        WorkPlanStep.objects.filter(plan=plan).delete()
        return list(dict.fromkeys(reasons))
    for raw in ordered:
        for dependency in raw["depends_on"]:
            WorkPlanStepDependency.objects.create(step=created[raw["key"]], depends_on=created[dependency])
    WorkPlanStep.objects.filter(plan=plan, dependency_links__isnull=True).update(status=WorkPlanStep.Status.READY, reason_codes=[])
    plan.is_composite = True
    plan.max_steps = maximum
    plan.worker_class = "composite"
    plan.operation = "composite"
    plan.input_spec = {"step_keys": [raw["key"] for raw in ordered]}
    return []


@transaction.atomic
def plan_awarded_job(job_id) -> WorkPlan:
    job = Job.objects.select_for_update().get(pk=job_id)
    if job.state not in {Job.State.CLAIMED, Job.State.AWARDED, Job.State.EXECUTING}:
        raise PlanningError(f"job is not in a plannable acquired state: {job.state}")
    plan, _ = WorkPlan.objects.select_for_update().get_or_create(job=job)
    if plan.status in {
        WorkPlan.Status.SUBMITTED,
        WorkPlan.Status.QA_PASSED,
        WorkPlan.Status.SUBMITTING,
        WorkPlan.Status.SUBMISSION_RECONCILIATION,
        WorkPlan.Status.EXECUTING,
        WorkPlan.Status.QUEUED,
        WorkPlan.Status.FAILED,
    }:
        return plan

    assets = list(JobAsset.objects.filter(job=job, status=JobAsset.Status.VERIFIED, duplicate_of=None).exclude(path="").order_by("created_at"))
    reasons: list[str] = []
    reasons.extend(evaluate_job(job).reason_codes)
    operation = ""
    input_spec = {}
    plan.is_composite = False
    plan.max_steps = 1
    raw_payload = job.normalized_payload if isinstance(job.normalized_payload, dict) else {}
    raw_steps = raw_payload.get("workflow_steps")
    manifest = rebuild_asset_manifest(job) if JobAsset.objects.filter(job=job).exists() else None
    if manifest and manifest.status != JobAssetManifest.Status.VERIFIED:
        reasons.extend(manifest.reason_codes)
    if isinstance(raw_steps, list):
        reasons.extend(_build_composite_plan(plan, job, assets, raw_steps))
        if not reasons:
            operation = "composite"
            input_spec = plan.input_spec
    elif raw_steps is not None:
        reasons.append("COMPOSITE_STEPS_INVALID")
    elif len(assets) > 1:
        reasons.append("MULTIPLE_INPUT_ASSETS_AMBIGUOUS")
    else:
        operation, input_spec, infer_reasons = _infer_operation(job, assets[0] if assets else None)
        reasons.extend(infer_reasons)

    if operation and operation != "composite":
        try:
            plan.worker_class = operation_spec(operation).worker_class
        except WorkerRegistryError:
            plan.worker_class = ""
            reasons.append("WORKER_OPERATION_NOT_REGISTERED")
    elif operation != "composite":
        plan.worker_class = ""
    plan.operation = operation
    plan.input_spec = input_spec
    if operation != "composite":
        plan.is_composite = False
        plan.max_steps = 1
        WorkPlanStep.objects.filter(plan=plan).delete()
    plan.status = WorkPlan.Status.READY if operation and not reasons else WorkPlan.Status.BLOCKED
    plan.planner_version = PLANNER_VERSION
    plan.reason_codes = reasons
    plan.max_repair_attempts = max(0, min(int(os.getenv("MAX_DETERMINISTIC_REPAIR_ATTEMPTS", "1")), 3))
    plan.last_error_code = ""
    plan.save()
    AuditEvent.objects.create(
        event_type="job.plan_ready" if plan.status == WorkPlan.Status.READY else "job.plan_blocked",
        actor="deterministic-planner",
        metadata={"job_id": str(job.id), "plan_id": plan.id, "operation": operation, "reason_codes": reasons},
    )
    return plan


def _queue_execution(plan: WorkPlan) -> bool:
    from control.services.admission import decide_admission
    from control.queueing import queue
    from control.tasks import execute_work_plan_task

    operation = plan.operation
    if plan.is_composite:
        step = plan.steps.filter(status__in=[WorkPlanStep.Status.READY, WorkPlanStep.Status.NEEDS_REPAIR]).order_by("sequence").first()
        operation = step.operation if step else ""
    decision = decide_admission(purpose="WORKPLAN_QUEUE", job=plan.job, operation=operation)
    if not decision.allowed:
        WorkPlan.objects.filter(pk=plan.pk).update(status=WorkPlan.Status.BLOCKED, reason_codes=decision.reason_codes)
        return False
    try:
        queue("p3").enqueue(
            execute_work_plan_task,
            plan.id,
            job_id=f"workplan:execute:{plan.id}:{plan.execution_attempts + 1}",
            result_ttl=86400,
            failure_ttl=604800,
        )
    except Exception as exc:
        AuditEvent.objects.create(
            severity="WARNING",
            event_type="job.plan_queue_failed",
            actor="planner",
            metadata={"job_id": str(plan.job_id), "plan_id": plan.id, "error_code": exc.__class__.__name__},
        )
        return False
    WorkPlan.objects.filter(
        pk=plan.pk,
        status__in=[WorkPlan.Status.READY, WorkPlan.Status.NEEDS_REPAIR],
    ).update(status=WorkPlan.Status.QUEUED, last_queued_at=timezone.now())
    return True


def reconcile_submission_plans(*, marketplace_slug: str | None = None, limit: int = 100) -> int:
    plans = WorkPlan.objects.select_related("job", "job__marketplace").filter(status=WorkPlan.Status.SUBMISSION_RECONCILIATION)
    if marketplace_slug:
        plans = plans.filter(job__marketplace__slug=marketplace_slug)
    reconciled = 0
    for plan in plans.order_by("updated_at")[: max(1, min(int(limit), 500))]:
        if plan.job.state == Job.State.SUBMITTED or Submission.objects.filter(job=plan.job, status="SUBMITTED").exists():
            WorkPlan.objects.filter(pk=plan.pk, status=WorkPlan.Status.SUBMISSION_RECONCILIATION).update(
                status=WorkPlan.Status.SUBMITTED,
                submitted_at=plan.submitted_at or timezone.now(),
                last_error_code="",
            )
            reconciled += 1
    return reconciled


def dispatch_awarded_jobs(*, marketplace_slug: str | None = None, limit: int = 50) -> dict:
    reconciled = reconcile_submission_plans(marketplace_slug=marketplace_slug, limit=limit)
    query = Job.objects.filter(state__in=[Job.State.CLAIMED, Job.State.AWARDED])
    if marketplace_slug:
        query = query.filter(marketplace__slug=marketplace_slug)
    queued = blocked = failed = 0
    for job in query.order_by("updated_at")[: max(1, min(int(limit), 200))]:
        try:
            plan = plan_awarded_job(job.id)
            if plan.status == WorkPlan.Status.READY:
                queued += int(_queue_execution(plan))
            elif plan.status == WorkPlan.Status.BLOCKED:
                blocked += 1
            elif plan.status == WorkPlan.Status.FAILED:
                failed += 1
        except Exception as exc:
            failed += 1
            AuditEvent.objects.create(
                severity="WARNING",
                event_type="job.planning_failed",
                actor="planner",
                metadata={"job_id": str(job.id), "error_code": exc.__class__.__name__},
            )
    submission_queued = 0
    submission_plans = WorkPlan.objects.select_related("job", "job__marketplace").filter(status=WorkPlan.Status.QA_PASSED)
    if marketplace_slug:
        submission_plans = submission_plans.filter(job__marketplace__slug=marketplace_slug)
    for plan in submission_plans.order_by("updated_at")[: max(1, min(int(limit), 200))]:
        submission_queued += int(_queue_submission(plan))
    return {
        "queued": queued,
        "blocked": blocked,
        "failed": failed,
        "submission_queued": submission_queued,
        "submission_reconciled": reconciled,
    }


def _unlock_composite_steps(plan: WorkPlan) -> None:
    for step in plan.steps.filter(status=WorkPlanStep.Status.BLOCKED).order_by("sequence"):
        dependencies = [link.depends_on for link in step.dependency_links.select_related("depends_on")]
        if dependencies and all(item.status == WorkPlanStep.Status.QA_PASSED for item in dependencies):
            step.status = WorkPlanStep.Status.READY
            step.reason_codes = []
            step.save(update_fields=["status", "reason_codes", "updated_at"])


def _composite_inputs(step: WorkPlanStep) -> dict:
    inputs = dict(step.input_spec)
    assets = list(step.input_assets.filter(status=JobAsset.Status.VERIFIED, duplicate_of=None).order_by("id"))
    dependencies = [link.depends_on for link in step.dependency_links.select_related("depends_on")]
    if any(item.status != WorkPlanStep.Status.QA_PASSED or item.qa_result_id is None or not item.qa_result.passed for item in dependencies):
        raise PlanningError("downstream step requires QA-passed upstream artifacts")
    artifacts = []
    for dependency in dependencies:
        artifacts.extend(list(dependency.output_artifacts.order_by("id")))
    step.input_artifacts.set(artifacts)
    paths = [row.path for row in assets if row.path] + [row.path for row in artifacts if row.path]
    inputs["sources"] = paths
    inputs["input_asset_ids"] = [row.id for row in assets]
    inputs["upstream_artifact_ids"] = [row.id for row in artifacts]
    if len(paths) == 1:
        inputs.setdefault("source", paths[0])
    elif len(paths) > 1 and "source_role" in inputs:
        role_assets = [row for row in assets if row.semantic_role == inputs["source_role"]]
        if len(role_assets) == 1:
            inputs.setdefault("source", role_assets[0].path)
    return inputs


def _execute_composite_plan(plan_id: int) -> WorkPlan:
    from control.services.execution import execute_registered_job

    initial = WorkPlan.objects.get(pk=plan_id)
    if initial.status not in {WorkPlan.Status.READY, WorkPlan.Status.QUEUED, WorkPlan.Status.EXECUTING, WorkPlan.Status.NEEDS_REPAIR}:
        return initial
    maximum_iterations = max(1, min(int(os.getenv("WORKPLAN_MAX_COMPOSITE_STEPS", "8")), 20))
    for _ in range(maximum_iterations):
        with transaction.atomic():
            plan = WorkPlan.objects.select_for_update().select_related("job", "job__marketplace").get(pk=plan_id)
            _unlock_composite_steps(plan)
            failed = plan.steps.filter(status__in=[WorkPlanStep.Status.FAILED]).first()
            if failed:
                plan.status = WorkPlan.Status.FAILED
                plan.reason_codes = [f"COMPOSITE_STEP_FAILED:{failed.key}"]
                plan.save(update_fields=["status", "reason_codes", "updated_at"])
                return plan
            if plan.steps.exists() and not plan.steps.exclude(status=WorkPlanStep.Status.QA_PASSED).exists():
                plan.status = WorkPlan.Status.QA_PASSED
                plan.reason_codes = []
                plan.save(update_fields=["status", "reason_codes", "updated_at"])
                if plan.job.marketplace.slug == "agentgigs":
                    _queue_submission(plan)
                return plan
            step = plan.steps.filter(status=WorkPlanStep.Status.NEEDS_REPAIR).order_by("sequence").first()
            repair = step is not None
            if step is None:
                step = plan.steps.filter(status=WorkPlanStep.Status.READY).order_by("sequence").first()
            if step is None:
                plan.status = WorkPlan.Status.BLOCKED
                plan.reason_codes = ["COMPOSITE_NO_EXECUTABLE_STEP"]
                plan.save(update_fields=["status", "reason_codes", "updated_at"])
                return plan
            if repair and step.repair_attempts >= step.max_repair_attempts:
                step.status = WorkPlanStep.Status.BLOCKED
                step.reason_codes = ["MAX_REPAIR_ATTEMPTS_REACHED"]
                step.save(update_fields=["status", "reason_codes", "updated_at"])
                plan.status = WorkPlan.Status.BLOCKED
                plan.reason_codes = [f"COMPOSITE_STEP_REPAIR_LIMIT:{step.key}"]
                plan.save(update_fields=["status", "reason_codes", "updated_at"])
                return plan
            inputs = _composite_inputs(step)
            step.attempt += 1
            if repair:
                step.repair_attempts += 1
                step.repair_history = [*step.repair_history, {"attempt": step.attempt, "reason_codes": step.reason_codes}]
                plan.repair_attempts += 1
            step.status = WorkPlanStep.Status.EXECUTING
            step.reason_codes = []
            step.save(update_fields=["attempt", "repair_attempts", "repair_history", "status", "reason_codes", "updated_at"])
            plan.execution_attempts += 1
            plan.status = WorkPlan.Status.EXECUTING
            plan.save(update_fields=["execution_attempts", "repair_attempts", "status", "updated_at"])
            job_id = plan.job_id
            worker_id = f"{step.worker_class}-{str(job_id)[:8]}-{step.key[:24]}"
            worker_class = step.worker_class
            step_id = step.id
            allow_repair = repair or plan.job.state == Job.State.EXECUTING

        try:
            execution = execute_registered_job(
                job_id=job_id,
                worker_id=worker_id,
                inputs=inputs,
                allow_repair=allow_repair,
                expected_worker_class=worker_class,
            )
        except Exception as exc:
            WorkPlanStep.objects.filter(pk=step_id).update(
                status=WorkPlanStep.Status.FAILED,
                reason_codes=[exc.__class__.__name__[:120]],
            )
            WorkPlan.objects.filter(pk=plan_id).update(
                status=WorkPlan.Status.FAILED,
                reason_codes=[f"COMPOSITE_STEP_FAILED:{step.key}"],
                last_error_code=exc.__class__.__name__[:120],
            )
            raise

        step = WorkPlanStep.objects.get(pk=step_id)
        qa = QAResult.objects.filter(execution=execution).order_by("-created_at").first()
        step.execution = execution
        step.qa_result = qa
        step_calls = list(GenXCall.objects.filter(
            job_id=job_id,
            worker_id=execution.worker_id,
            created_at__gte=execution.started_at,
            created_at__lte=execution.ended_at or timezone.now(),
        ))
        step.actual_cost = sum((call.cost_equivalent or Decimal("0") for call in step_calls), Decimal("0"))
        step.output_artifacts.set(execution.artifacts.all())
        if any(call.cost_equivalent is None for call in step_calls):
            step.status = WorkPlanStep.Status.BLOCKED
            step.reason_codes = ["GENX_MONETARY_COST_UNRESOLVED"]
            step.save(update_fields=["execution", "qa_result", "actual_cost", "status", "reason_codes", "updated_at"])
            WorkPlan.objects.filter(pk=plan_id).update(status=WorkPlan.Status.BLOCKED, reason_codes=[f"COMPOSITE_STEP_COST_UNRESOLVED:{step.key}"])
            return WorkPlan.objects.get(pk=plan_id)
        if execution.status == "QA_PASSED" and qa and qa.passed:
            step.status = WorkPlanStep.Status.QA_PASSED
            step.reason_codes = []
            step.save(update_fields=["execution", "qa_result", "actual_cost", "status", "reason_codes", "updated_at"])
            continue
        step.status = WorkPlanStep.Status.NEEDS_REPAIR
        step.reason_codes = ["DETERMINISTIC_QA_FAILED"]
        step.save(update_fields=["execution", "qa_result", "actual_cost", "status", "reason_codes", "updated_at"])
        WorkPlan.objects.filter(pk=plan_id).update(status=WorkPlan.Status.NEEDS_REPAIR, reason_codes=[f"COMPOSITE_STEP_QA_FAILED:{step.key}"])
        return WorkPlan.objects.get(pk=plan_id)
    WorkPlan.objects.filter(pk=plan_id).update(status=WorkPlan.Status.BLOCKED, reason_codes=["COMPOSITE_STEP_LIMIT"])
    return WorkPlan.objects.get(pk=plan_id)


def execute_work_plan(plan_id: int) -> WorkPlan:
    from control.services.execution import execute_registered_job

    if WorkPlan.objects.filter(pk=plan_id, is_composite=True).exists():
        return _execute_composite_plan(plan_id)

    with transaction.atomic():
        plan = WorkPlan.objects.select_for_update().select_related("job", "job__marketplace").get(pk=plan_id)
        if plan.status not in {WorkPlan.Status.READY, WorkPlan.Status.QUEUED, WorkPlan.Status.NEEDS_REPAIR}:
            return plan
        repair = plan.status == WorkPlan.Status.NEEDS_REPAIR
        prohibited = evaluate_job(plan.job)
        if not prohibited.allowed:
            plan.status = WorkPlan.Status.BLOCKED
            plan.reason_codes = list(prohibited.reason_codes)
            plan.save(update_fields=["status", "reason_codes", "updated_at"])
            return plan
        if repair and plan.repair_attempts >= plan.max_repair_attempts:
            plan.status = WorkPlan.Status.BLOCKED
            plan.reason_codes = [*plan.reason_codes, "MAX_REPAIR_ATTEMPTS_REACHED"]
            plan.save(update_fields=["status", "reason_codes", "updated_at"])
            return plan
        if repair and plan.max_repair_cost > 0:
            prior_repairs = Execution.objects.filter(job=plan.job, attempt__gt=1)
            repair_cost = Decimal("0")
            for prior in prior_repairs:
                if prior.started_at is None:
                    continue
                prior_calls = list(GenXCall.objects.filter(
                    job=plan.job,
                    created_at__gte=prior.started_at,
                    created_at__lte=prior.ended_at or timezone.now(),
                ))
                if any(call.cost_equivalent is None for call in prior_calls):
                    plan.status = WorkPlan.Status.BLOCKED
                    plan.reason_codes = [*plan.reason_codes, "GENX_MONETARY_COST_UNRESOLVED"]
                    plan.save(update_fields=["status", "reason_codes", "updated_at"])
                    return plan
                repair_cost += sum((call.cost_equivalent or Decimal("0") for call in prior_calls), Decimal("0"))
            estimated_next = getattr(getattr(plan.job, "jobscore", None), "expected_genx_cost", Decimal("0"))
            if repair_cost + estimated_next > plan.max_repair_cost:
                plan.status = WorkPlan.Status.BLOCKED
                plan.reason_codes = [*plan.reason_codes, "REPAIR_ECONOMIC_BUDGET_EXCEEDED"]
                plan.save(update_fields=["status", "reason_codes", "updated_at"])
                return plan
        plan.execution_attempts += 1
        if repair:
            plan.repair_attempts += 1
        plan.status = WorkPlan.Status.EXECUTING
        plan.last_error_code = ""
        plan.save(update_fields=["execution_attempts", "repair_attempts", "status", "last_error_code", "updated_at"])
        job_id = plan.job_id
        inputs = dict(plan.input_spec)
        if repair:
            inputs["repair_attempt"] = plan.repair_attempts
            inputs["max_repair_cost"] = str(plan.max_repair_cost)
        worker_id = f"{plan.worker_class}-{str(job_id)[:8]}"
        worker_class = plan.worker_class

    try:
        execution = execute_registered_job(
            job_id=job_id,
            worker_id=worker_id,
            inputs=inputs,
            allow_repair=repair,
            expected_worker_class=worker_class,
        )
    except Exception as exc:
        WorkPlan.objects.filter(pk=plan_id).update(status=WorkPlan.Status.FAILED, last_error_code=exc.__class__.__name__[:120])
        raise

    plan = WorkPlan.objects.get(pk=plan_id)
    if execution.status == "QA_PASSED":
        plan.status = WorkPlan.Status.QA_PASSED
        plan.reason_codes = []
        plan.save(update_fields=["status", "reason_codes", "updated_at"])
        if plan.job.marketplace.slug == "agentgigs":
            _queue_submission(plan)
    elif execution.status == "NEEDS_REPAIR":
        if plan.repair_attempts >= plan.max_repair_attempts:
            plan.status = WorkPlan.Status.BLOCKED
            plan.reason_codes = ["MAX_REPAIR_ATTEMPTS_REACHED"]
            plan.save(update_fields=["status", "reason_codes", "updated_at"])
        else:
            plan.status = WorkPlan.Status.NEEDS_REPAIR
            plan.reason_codes = ["DETERMINISTIC_QA_FAILED"]
            plan.save(update_fields=["status", "reason_codes", "updated_at"])
            _queue_execution(plan)
    return WorkPlan.objects.get(pk=plan_id)


def _queue_submission(plan: WorkPlan) -> bool:
    from control.queueing import queue
    from control.tasks import submit_work_plan_task

    try:
        queue("p0").enqueue(
            submit_work_plan_task,
            plan.id,
            job_id=f"workplan:submit:{plan.id}",
            result_ttl=86400,
            failure_ttl=604800,
        )
        WorkPlan.objects.filter(pk=plan.pk, status=WorkPlan.Status.QA_PASSED).update(status=WorkPlan.Status.SUBMITTING, last_queued_at=timezone.now())
        return True
    except Exception as exc:
        AuditEvent.objects.create(
            severity="WARNING",
            event_type="job.submission_queue_failed",
            actor="planner",
            metadata={"job_id": str(plan.job_id), "plan_id": plan.id, "error_code": exc.__class__.__name__},
        )
        return False


def submit_work_plan(plan_id: int) -> WorkPlan:
    from control.services.agentgigs import configured_adapter
    from control.services.submission import submit_qa_passed_job

    plan = WorkPlan.objects.select_related("job", "job__marketplace").get(pk=plan_id)
    if plan.status == WorkPlan.Status.SUBMITTED:
        return plan
    if plan.status not in {WorkPlan.Status.QA_PASSED, WorkPlan.Status.SUBMITTING}:
        raise PlanningError(f"work plan is not ready for submission: {plan.status}")
    if plan.job.marketplace.slug != "agentgigs":
        plan.status = WorkPlan.Status.BLOCKED
        plan.reason_codes = ["SUBMISSION_ADAPTER_NOT_AUTOMATED"]
        plan.save(update_fields=["status", "reason_codes", "updated_at"])
        return plan
    try:
        submit_qa_passed_job(adapter=configured_adapter(), job_id=plan.job_id)
    except Exception as exc:
        if Submission.objects.filter(job_id=plan.job_id, status="UNKNOWN_REMOTE_STATE").exists():
            plan.status = WorkPlan.Status.SUBMISSION_RECONCILIATION
            plan.last_error_code = exc.__class__.__name__[:120]
            plan.save(update_fields=["status", "last_error_code", "updated_at"])
            return plan
        plan.status = WorkPlan.Status.FAILED
        plan.last_error_code = exc.__class__.__name__[:120]
        plan.save(update_fields=["status", "last_error_code", "updated_at"])
        raise
    plan.status = WorkPlan.Status.SUBMITTED
    plan.submitted_at = timezone.now()
    plan.save(update_fields=["status", "submitted_at", "updated_at"])
    return plan
