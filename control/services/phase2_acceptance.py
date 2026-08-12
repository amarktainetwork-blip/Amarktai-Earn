from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from control.services.integration_accounts import BY_SLUG
from workers.registry import capability_coverage, operation_contract, registered_operations


@dataclass(frozen=True)
class WorkflowRequirement:
    name: str
    family: str
    operations: tuple[str, ...]
    proof_files: tuple[str, ...] = ()


WORKER_FAMILIES = {
    "structured_data": "data",
    "documents": "documents",
    "research": "research",
    "localization": "language_intelligence",
    "transcription": "audio",
    "media": "video",
    "advanced_structured_data": "data",
    "spreadsheet_reporting": "data",
    "data_analysis": "data",
    "technical_documentation": "language_intelligence",
    "content_copy": "language_intelligence",
    "seo_audit": "commercial_specialists",
    "presentations": "documents",
    "document_production": "documents",
    "public_web_data": "web_browser",
    "web_output": "code_software",
    "defensive_code_review": "code_software",
    "customer_support": "commercial_specialists",
    "code_small": "code_software",
    "code_heavy": "code_software",
    "ci_testing": "code_software",
    "synthetic_data": "data",
    "ai_safety_research": "commercial_specialists",
    "image_product": "vision",
    "ocr": "documents",
    "intelligence": "language_intelligence",
    "structured_semantic": "language_intelligence",
    "vision": "vision",
    "generated_media": "media_generation",
}


REQUIRED_FAMILIES = {
    "language_intelligence",
    "research",
    "code_software",
    "web_browser",
    "documents",
    "data",
    "vision",
    "audio",
    "video",
    "media_generation",
    "composite",
    "commercial_specialists",
}


REQUIRED_OPERATION_GROUPS = {
    "chat": ("intelligence_chat",),
    "reasoning": ("intelligence_reason",),
    "question_answering": ("intelligence_qa",),
    "summarization": ("intelligence_summarize", "document_summarize"),
    "rewriting": ("intelligence_rewrite", "document_rewrite"),
    "analysis": ("intelligence_analyze", "data_analysis_report"),
    "structured_json": ("structured_json_generate",),
    "classification": ("classify_text",),
    "fact_extraction": ("extract_structured_facts",),
    "translation_localization": ("translate_document",),
    "web_research": ("research_report",),
    "competitor_research": ("competitor_research",),
    "market_research": ("market_research",),
    "website_research": ("website_research",),
    "multi_source_research": ("multi_source_research",),
    "cited_fact_research": ("fact_extraction_research",),
    "code_generation_and_edits": ("code_change_small", "code_change_heavy"),
    "independent_tests": ("run_repository_tests",),
    "website_artifact": ("static_html_create",),
    "public_web_crawl_extract": ("public_web_extract",),
    "document_text_extraction": ("document_extract_text",),
    "scanned_document_ocr": ("ocr_document",),
    "docx_generation": ("docx_create",),
    "pdf_generation": ("pdf_create",),
    "presentation_generation": ("presentation_create",),
    "spreadsheet_generation": ("spreadsheet_report",),
    "tabular_transformations": ("tabular_convert", "tabular_normalize", "tabular_deduplicate"),
    "image_generation": ("image_generate_product_asset",),
    "image_editing": ("image_edit_product_asset",),
    "image_understanding": ("vision_understand",),
    "image_ocr": ("vision_ocr",),
    "visual_qa": ("vision_qa",),
    "speech_to_text": ("transcribe_media",),
    "text_to_speech": ("voice_generate",),
    "text_to_audio": ("audio_generate",),
    "music_generation": ("music_generate",),
    "text_to_video": ("video_generate",),
    "image_to_video": ("image_to_video",),
    "media_processing": ("media_trim", "media_transcode", "media_extract_audio"),
    "video_assembly": ("media_concat",),
}


WORKFLOWS = (
    WorkflowRequirement(
        "research_to_document",
        "composite",
        ("research_report", "docx_create", "pdf_create"),
        ("tests/test_multifile_composite_integration.py",),
    ),
    WorkflowRequirement(
        "website_to_brand_package",
        "composite",
        ("public_web_extract", "extract_structured_facts", "content_package", "image_generate_product_asset"),
        ("tests/test_multifile_composite_integration.py",),
    ),
    WorkflowRequirement(
        "website_to_marketing_campaign",
        "composite",
        ("public_web_extract", "content_package", "image_generate_product_asset", "video_generate", "voice_generate"),
        ("tests/test_multifile_composite_integration.py",),
    ),
    WorkflowRequirement(
        "research_to_spreadsheet_to_presentation",
        "composite",
        ("research_report", "data_analysis_report", "spreadsheet_report", "presentation_create"),
        ("tests/test_multifile_composite_integration.py",),
    ),
    WorkflowRequirement(
        "code_to_test_to_application_artifact",
        "composite",
        ("code_change_heavy", "run_repository_tests", "static_html_create"),
        ("tests/test_multifile_composite_integration.py", "tests/test_phase8c_coding_integration.py"),
    ),
    WorkflowRequirement(
        "long_form_video_workflow",
        "video",
        ("video_generate", "media_concat", "media_transcode"),
        ("tests/test_phase2_capability_factory_integration.py",),
    ),
    WorkflowRequirement(
        "document_question_answering",
        "documents",
        ("document_extract_text", "intelligence_qa"),
    ),
    WorkflowRequirement(
        "audio_translation_workflow",
        "audio",
        ("transcribe_media", "translate_document"),
    ),
)


def _operation_rows(coverage: dict) -> tuple[list[dict], set[str]]:
    by_operation = {row["operation"]: row for row in coverage.get("operations", [])}
    rows = []
    families = set()
    for operation in registered_operations():
        contract = operation_contract(operation)
        family = WORKER_FAMILIES.get(contract.worker_class, "")
        if family:
            families.add(family)
        proof = by_operation.get(operation)
        if not family:
            status = "FAIL"
            reason = "REGISTERED_OPERATION_HAS_NO_PHASE2_FAMILY"
        elif not proof or proof.get("status") != "PASS":
            status = "FAIL"
            reason = "REGISTERED_OPERATION_CONTRACT_PROOF_FAILED"
        elif contract.owner_action_blocker:
            status = "READY_FOR_CREDENTIAL"
            reason = contract.owner_action_blocker
        else:
            status = "PASS"
            reason = "LOCAL_OR_CREDENTIAL_FREE_CONTRACT_PROVEN"
        rows.append({
            "kind": "operation",
            "name": operation,
            "family": family or "UNMAPPED",
            "status": status,
            "engineering_state": "READY_FOR_LIVE_PROOF" if status == "READY_FOR_CREDENTIAL" else "LOCAL_PROVEN" if status == "PASS" else "BLOCKED",
            "worker_class": contract.worker_class,
            "provider_category": contract.provider_category,
            "qa_profile": contract.qa_profile,
            "proof_class": contract.proof_class,
            "reason": reason,
        })
    return rows, families


def _group_rows(operation_names: set[str]) -> list[dict]:
    rows = []
    for name, operations in REQUIRED_OPERATION_GROUPS.items():
        missing = [operation for operation in operations if operation not in operation_names]
        if missing:
            status = "FAIL"
            reason = {"missing_operations": missing}
        else:
            statuses = [
                "READY_FOR_CREDENTIAL" if operation_contract(operation).owner_action_blocker else "PASS"
                for operation in operations
            ]
            status = "READY_FOR_CREDENTIAL" if "READY_FOR_CREDENTIAL" in statuses else "PASS"
            reason = {"operations": list(operations)}
        rows.append({"kind": "required_capability", "name": name, "status": status, "details": reason})
    return rows


def _workflow_rows(root: Path, operation_names: set[str]) -> tuple[list[dict], set[str]]:
    rows = []
    families = set()
    for workflow in WORKFLOWS:
        families.add(workflow.family)
        missing_operations = [operation for operation in workflow.operations if operation not in operation_names]
        missing_proofs = [path for path in workflow.proof_files if not (root / path).is_file()]
        if missing_operations or missing_proofs:
            status = "FAIL"
        elif any(operation_contract(operation).owner_action_blocker for operation in workflow.operations):
            status = "READY_FOR_CREDENTIAL"
        else:
            status = "PASS"
        rows.append({
            "kind": "workflow",
            "name": workflow.name,
            "family": workflow.family,
            "status": status,
            "operations": list(workflow.operations),
            "missing_operations": missing_operations,
            "missing_proof_files": missing_proofs,
        })
    return rows, families


def _browser_tool_row(root: Path) -> dict:
    source = root / "integrations" / "apify_actor" / "src" / "main.py"
    requirements = root / "integrations" / "apify_actor" / "requirements.txt"
    dockerfile = root / "integrations" / "apify_actor" / ".actor" / "Dockerfile"
    schema = root / "integrations" / "apify_actor" / ".actor" / "input_schema.json"
    checks = {
        "source_exists": source.is_file(),
        "requirements_exists": requirements.is_file(),
        "dockerfile_exists": dockerfile.is_file(),
        "input_schema_exists": schema.is_file(),
        "apify_integration_defined": "apify-store" in BY_SLUG,
    }
    if all(checks.values()):
        source_text = source.read_text(encoding="utf-8", errors="replace")
        requirements_text = requirements.read_text(encoding="utf-8", errors="replace").casefold()
        docker_text = dockerfile.read_text(encoding="utf-8", errors="replace").casefold()
        schema_text = schema.read_text(encoding="utf-8", errors="replace")
        for mode in ("browser_snapshot", "browser_extract", "form_inspect", "form_fill_preview"):
            checks[f"mode_{mode}"] = mode in source_text and mode in schema_text
        checks.update({
            "explicit_browser_authorization": "authorization_confirmed" in source_text,
            "generic_form_submission_disabled": '"form_submitted": False' in source_text and "form.submit(" not in source_text,
            "playwright_runtime_declared": "playwright" in requirements_text,
            "chromium_installed_offhost": "playwright install --with-deps chromium" in docker_text,
            "actor_compiles_during_build": "compileall" in docker_text,
        })
    definition = BY_SLUG.get("apify-store")
    if definition:
        required_capabilities = {"ACTOR_MAPPING", "RUN_RECONCILIATION", "COST_INGESTION"}
        checks["run_and_cost_reconciliation_contract"] = required_capabilities.issubset(set(definition.capabilities))
        checks["heavy_execution_offhost"] = "HEAVY_EXECUTION_ON_APIFY" in definition.off_host_requirements
    return {
        "kind": "external_tool",
        "name": "authorized_interactive_browser",
        "family": "web_browser",
        "status": "READY_FOR_CREDENTIAL" if checks and all(checks.values()) else "FAIL",
        "activation": "APIFY_API_TOKEN_AND_ACTOR_MAPPING",
        "checks": checks,
    }


def _ocr_runtime_row(root: Path) -> dict:
    dockerfile = root / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8", errors="replace").casefold() if dockerfile.is_file() else ""
    checks = {
        "production_dockerfile_exists": dockerfile.is_file(),
        "tesseract_installed": "tesseract-ocr" in text,
        "poppler_installed": "poppler-utils" in text,
        "ocr_operation_registered": "ocr_document" in set(registered_operations()),
    }
    return {
        "kind": "runtime",
        "name": "local_scanned_document_ocr_runtime",
        "family": "documents",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
    }


def phase2_acceptance_report(*, repository_root: Path | None = None) -> dict:
    root = repository_root or Path(__file__).resolve().parents[2]
    coverage = capability_coverage(repository_root=root)
    operation_names = set(registered_operations())
    operation_rows, operation_families = _operation_rows(coverage)
    group_rows = _group_rows(operation_names)
    workflow_rows, workflow_families = _workflow_rows(root, operation_names)
    browser_row = _browser_tool_row(root)
    ocr_row = _ocr_runtime_row(root)
    families = operation_families | workflow_families | {browser_row["family"], ocr_row["family"]}
    missing_families = sorted(REQUIRED_FAMILIES - families)
    family_rows = [
        {
            "kind": "family",
            "name": family,
            "status": "PASS" if family in families else "FAIL",
        }
        for family in sorted(REQUIRED_FAMILIES)
    ]
    rows = operation_rows + group_rows + workflow_rows + [browser_row, ocr_row] + family_rows
    counts = {
        "TOTAL": len(rows),
        "PASS": sum(row["status"] == "PASS" for row in rows),
        "READY_FOR_CREDENTIAL": sum(row["status"] == "READY_FOR_CREDENTIAL" for row in rows),
        "FAIL": sum(row["status"] == "FAIL" for row in rows),
        "PARTIAL": 0,
        "UNKNOWN": 0,
        "UNREGISTERED": 0,
        "REGISTERED_OPERATIONS": len(operation_names),
    }
    if coverage.get("status") != "PASS":
        counts["FAIL"] += 1
    if missing_families:
        counts["FAIL"] += len(missing_families)
    return {
        "phase": 2,
        "name": "COMPLETE_AND_ACCEPT_ALL_CAPABILITIES",
        "status": "PASS" if counts["FAIL"] == 0 else "FAIL",
        "summary": counts,
        "registry_summary": coverage.get("summary") or {},
        "missing_families": missing_families,
        "rows": rows,
        "note": "No paid provider call is performed. GenX-backed and Apify-backed capabilities may pass engineering acceptance as READY_FOR_CREDENTIAL; live activation remains a later owner credential proof.",
    }
