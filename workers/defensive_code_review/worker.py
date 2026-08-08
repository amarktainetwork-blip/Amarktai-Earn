from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from workers.base import WorkRequest, WorkResult, Worker


PATTERNS = (
    ("POTENTIAL_EVAL_USAGE", re.compile(r"\b(?:eval|exec)\s*\(")),
    ("POTENTIAL_SHELL_EXECUTION", re.compile(r"\b(?:shell\s*=\s*True|os\.system\s*\()")),
    ("POTENTIAL_HARDCODED_SECRET", re.compile(r"(?i)(?:api[_-]?key|secret|password)\s*[:=]\s*['\"][^'\"]{12,}")),
    ("POTENTIAL_WEAK_HASH", re.compile(r"\b(?:md5|sha1)\s*\(", re.IGNORECASE)),
    ("TODO_OR_FIXME", re.compile(r"\b(?:TODO|FIXME)\b")),
)


class DefensiveCodeReviewWorker(Worker):
    worker_class = "defensive_code_review"

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            if request.inputs.get("operation") != "defensive_code_review":
                return WorkResult(ok=False, error="unsupported defensive-review operation")
            if request.inputs.get("authorization_confirmed") is not True:
                return WorkResult(ok=False, error="DEFENSIVE_REVIEW_AUTHORIZATION_REQUIRED")
            scope = str(request.inputs.get("scope") or "").strip()
            if not scope:
                return WorkResult(ok=False, error="DEFENSIVE_REVIEW_SCOPE_REQUIRED")
            repository = Path(str(request.inputs["repository_path"]))
            maximum_files = max(1, min(int(os.getenv("DEFENSIVE_REVIEW_MAX_FILES", "2000")), 10000))
            maximum_bytes = max(1024, int(os.getenv("DEFENSIVE_REVIEW_MAX_FILE_BYTES", str(2 * 1024 * 1024))))
            ignored = {".git", ".venv", "node_modules", "vendor", "dist", "build", "__pycache__"}
            suffixes = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".cs", ".rb", ".php", ".sh", ".yaml", ".yml", ".json", ".toml"}
            findings = []; scanned = 0; extensions = Counter(); test_files = 0
            observed_names = set()
            for candidate in sorted(repository.rglob("*")):
                if scanned >= maximum_files: break
                if not candidate.is_file() or candidate.is_symlink() or any(part in ignored for part in candidate.parts): continue
                if candidate.suffix.casefold() not in suffixes or candidate.stat().st_size > maximum_bytes: continue
                relative = candidate.relative_to(repository).as_posix(); scanned += 1; extensions[candidate.suffix.casefold()] += 1
                observed_names.add(candidate.name.casefold())
                if "test" in candidate.name.casefold() or "tests" in candidate.parts: test_files += 1
                text = candidate.read_text(encoding="utf-8", errors="replace")
                for code, pattern in PATTERNS:
                    for match in list(pattern.finditer(text))[:20]:
                        findings.append({"code": code, "file": relative, "line": text.count("\n", 0, match.start()) + 1})
                if candidate.name.casefold() in {"requirements.txt", "requirements-prod.txt"}:
                    for line_number, line in enumerate(text.splitlines(), start=1):
                        stripped = line.strip()
                        if stripped and not stripped.startswith(("#", "-")) and not any(token in stripped for token in ("==", ">=", "<=", "~=", " @ ")):
                            findings.append({"code": "UNBOUNDED_DEPENDENCY", "file": relative, "line": line_number})
                if candidate.name.casefold() == ".env":
                    findings.append({"code": "SENSITIVE_ENV_FILE_PRESENT", "file": relative, "line": 1})
            if not test_files: findings.append({"code": "NO_TEST_FILES_OBSERVED", "file": "", "line": 0})
            if "package.json" in observed_names and not observed_names.intersection({"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}):
                findings.append({"code": "DEPENDENCY_LOCKFILE_MISSING", "file": "package.json", "line": 1})
            if scanned >= maximum_files: findings.append({"code": "REVIEW_FILE_LIMIT_REACHED", "file": "", "line": 0})
            request.workspace.mkdir(parents=True, exist_ok=True)
            target = request.workspace / "defensive-code-review.md"
            lines = [
                "# Defensive Code Review", "", f"Authorized scope: {scope}", "",
                "## Coverage", "", f"- Files inspected: {scanned}", f"- Test files observed: {test_files}",
                f"- File types: {dict(extensions)}", "", "## Findings", "",
            ]
            if findings:
                lines.extend(f"- {row['code']}: `{row['file']}` line {row['line']}" for row in findings[:1000])
            else:
                lines.append("- No pattern-level findings in the bounded review. This is not a guarantee of absence.")
            lines.extend(["", "## Boundaries", "", "Static, defensive, supplied-repository review only. No target interaction or exploitation was performed."])
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return WorkResult(ok=True, artifacts=[target], evidence={
                "operation": "defensive_code_review", "files_scanned": scanned, "finding_count": len(findings),
                "test_files": test_files, "authorization_confirmed": True, "scope": scope,
                "network_testing_performed": False, "exploitation_performed": False,
            })
        except (OSError, KeyError, TypeError, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))
