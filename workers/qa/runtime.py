from __future__ import annotations

import re
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


def _text_file(primary: Path) -> str:
    try:
        return primary.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _document_outcome(primary: Path, evidence: dict[str, Any]) -> QAOutcome:
    text = _text_file(primary)
    checks: list[str] = []
    source_chars = int(evidence.get("source_chars") or 0)
    operation = str(evidence.get("operation") or "")
    if text:
        checks.append("output_nonempty")
    if source_chars > 0:
        checks.append("source_text_present")

    passed = bool(text) and source_chars > 0
    if operation == "document_extract_text":
        if source_chars and len(text) >= max(1, int(source_chars * 0.8)):
            checks.append("extraction_length_consistent")
        passed = passed and "extraction_length_consistent" in checks
    elif operation in {"document_summarize", "document_rewrite"}:
        minimum = min(40, max(10, source_chars // 10))
        if len(text) >= minimum:
            checks.append("generated_document_has_content")
        passed = passed and "generated_document_has_content" in checks
    else:
        passed = False

    return QAOutcome(
        passed=passed,
        check_type="document_structural",
        score=1.0 if passed else 0.0,
        checks=checks,
        evidence={"operation": operation, "output_chars": len(text), "source_chars": source_chars},
    )


def _research_outcome(primary: Path, evidence: dict[str, Any]) -> QAOutcome:
    text = _text_file(primary)
    checks: list[str] = []
    sources = [str(url).rstrip(".,;") for url in evidence.get("sources", []) if str(url).startswith("https://")]
    inline = [url.rstrip(".,;") for url in re.findall(r"https://[^\s)\]>]+", text)]
    unique_sources = list(dict.fromkeys([*sources, *inline]))
    if len(text) >= 100:
        checks.append("report_has_substance")
    if len(unique_sources) >= 2:
        checks.append("multiple_https_sources")
    if "sources" in text.casefold() or inline:
        checks.append("citations_present")
    passed = all(name in checks for name in ("report_has_substance", "multiple_https_sources", "citations_present"))
    return QAOutcome(
        passed=passed,
        check_type="research_citation_structural",
        score=1.0 if passed else 0.0,
        checks=checks,
        evidence={"source_count": len(unique_sources), "sources": unique_sources[:20], "output_chars": len(text)},
    )


def _translation_outcome(primary: Path, evidence: dict[str, Any]) -> QAOutcome:
    text = _text_file(primary)
    checks: list[str] = []
    target_language = str(evidence.get("target_language") or "").strip()
    source_chars = int(evidence.get("source_chars") or 0)
    if text:
        checks.append("output_nonempty")
    if target_language:
        checks.append("target_language_explicit")
    if source_chars > 0:
        checks.append("source_text_present")
    if source_chars and text:
        ratio = len(text) / source_chars
        if 0.20 <= ratio <= 5.0:
            checks.append("translation_length_plausible")
    passed = all(name in checks for name in ("output_nonempty", "target_language_explicit", "source_text_present", "translation_length_plausible"))
    return QAOutcome(
        passed=passed,
        check_type="translation_structural",
        score=1.0 if passed else 0.0,
        checks=checks,
        evidence={"target_language": target_language, "source_chars": source_chars, "output_chars": len(text)},
    )


def _transcript_outcome(primary: Path, evidence: dict[str, Any]) -> QAOutcome:
    text = _text_file(primary)
    checks: list[str] = []
    words = len(text.split())
    if text:
        checks.append("output_nonempty")
    if words >= 3:
        checks.append("transcript_has_words")
    passed = "output_nonempty" in checks and "transcript_has_words" in checks
    return QAOutcome(
        passed=passed,
        check_type="transcript_structural",
        score=1.0 if passed else 0.0,
        checks=checks,
        evidence={"word_count": words, "output_chars": len(text)},
    )


def _code_patch_outcome(primary: Path, evidence: dict[str, Any]) -> QAOutcome:
    text = _text_file(primary)
    checks: list[str] = []
    if text and ("diff --git" in text or ("--- " in text and "+++ " in text)):
        checks.append("patch_nonempty")
    if int(evidence.get("agent_exit_code", 1)) == 0:
        checks.append("agent_completed")
    if int(evidence.get("test_exit_code", 1)) == 0:
        checks.append("independent_tests_passed")
    passed = all(name in checks for name in ("patch_nonempty", "agent_completed", "independent_tests_passed"))
    return QAOutcome(
        passed=passed,
        check_type="sandbox_code_patch",
        score=1.0 if passed else 0.0,
        checks=checks,
        evidence={"patch_chars": len(text), "agent_exit_code": evidence.get("agent_exit_code"), "test_exit_code": evidence.get("test_exit_code"), "sandbox_id": evidence.get("sandbox_id")},
    )


def _ci_outcome(primary: Path, evidence: dict[str, Any]) -> QAOutcome:
    text = _text_file(primary)
    checks: list[str] = []
    if text:
        checks.append("test_report_present")
    if int(evidence.get("test_exit_code", 1)) == 0:
        checks.append("tests_passed")
    passed = "test_report_present" in checks and "tests_passed" in checks
    return QAOutcome(
        passed=passed,
        check_type="sandbox_ci",
        score=1.0 if passed else 0.0,
        checks=checks,
        evidence={"report_chars": len(text), "test_exit_code": evidence.get("test_exit_code"), "sandbox_id": evidence.get("sandbox_id")},
    )


def run_qa(profile: str, primary: Path, worker_evidence: dict[str, Any] | None = None) -> QAOutcome:
    evidence = worker_evidence if isinstance(worker_evidence, dict) else {}
    if profile == "csv":
        result = verify_csv(primary, expected_rows=evidence.get("rows"), required_columns=evidence.get("columns"))
        return QAOutcome(
            passed=result.passed,
            check_type="deterministic_csv",
            score=1.0 if result.passed else 0.0,
            checks=list(result.checks),
            evidence=dict(result.evidence),
        )
    if profile == "document":
        return _document_outcome(primary, evidence)
    if profile == "research":
        return _research_outcome(primary, evidence)
    if profile == "translation":
        return _translation_outcome(primary, evidence)
    if profile == "transcript":
        return _transcript_outcome(primary, evidence)
    if profile == "code_patch":
        return _code_patch_outcome(primary, evidence)
    if profile == "ci":
        return _ci_outcome(primary, evidence)
    raise ValueError(f"unsupported QA profile: {profile}")
