from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WorkRequest:
    job_id: str
    workspace: Path
    inputs: dict[str, Any]
    worker_id: str = ""
    execution_id: int | None = None
    attempt: int = 1


@dataclass
class WorkResult:
    ok: bool
    artifacts: list[Path] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class Worker(ABC):
    worker_class: str

    @abstractmethod
    def execute(self, request: WorkRequest) -> WorkResult: ...

    def recover_completed_provider_result(self, request: WorkRequest) -> WorkResult:
        """Materialize an already-paid provider result without replaying a mutation.

        Provider-backed workers opt in explicitly. The default is fail-closed so
        recovery can never silently guess a capability-specific output format.
        """
        return WorkResult(ok=False, error="completed provider result recovery is not supported by this worker")
