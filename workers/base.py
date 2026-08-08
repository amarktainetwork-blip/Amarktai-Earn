from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass
class WorkRequest:
    job_id: str
    workspace: Path
    inputs: dict[str, Any]

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
