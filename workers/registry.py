from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Iterable


class WorkerRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerSpec:
    worker_class: str
    version: str
    factory: str
    operations: tuple[str, ...]
    qa_profile: str
    description: str

    def build(self):
        module_name, attr = self.factory.rsplit(".", 1)
        factory = getattr(import_module(module_name), attr)
        return factory()


_SPECS: tuple[WorkerSpec, ...] = (
    WorkerSpec(
        worker_class="structured_data",
        version="1.0.0",
        factory="workers.structured_data.worker.StructuredDataWorker",
        operations=("json_to_csv", "csv_normalize"),
        qa_profile="csv",
        description="Deterministic JSON/CSV conversion and normalization",
    ),
)

_BY_CLASS = {spec.worker_class: spec for spec in _SPECS}
_BY_OPERATION = {operation: spec for spec in _SPECS for operation in spec.operations}

if len(_BY_CLASS) != len(_SPECS):
    raise RuntimeError("duplicate worker_class in worker registry")
if sum(len(spec.operations) for spec in _SPECS) != len(_BY_OPERATION):
    raise RuntimeError("duplicate operation in worker registry")


def all_specs() -> tuple[WorkerSpec, ...]:
    return _SPECS


def worker_spec(worker_class: str) -> WorkerSpec:
    try:
        return _BY_CLASS[worker_class]
    except KeyError as exc:
        raise WorkerRegistryError(f"unknown worker class: {worker_class}") from exc


def operation_spec(operation: str) -> WorkerSpec:
    try:
        return _BY_OPERATION[operation]
    except KeyError as exc:
        raise WorkerRegistryError(f"unsupported worker operation: {operation}") from exc


def supports_operation(worker_class: str, operation: str) -> bool:
    spec = _BY_CLASS.get(worker_class)
    return bool(spec and operation in spec.operations)


def registered_operations() -> tuple[str, ...]:
    return tuple(sorted(_BY_OPERATION))


def registry_manifest() -> list[dict[str, object]]:
    return [
        {
            "worker_class": spec.worker_class,
            "version": spec.version,
            "operations": list(spec.operations),
            "qa_profile": spec.qa_profile,
            "description": spec.description,
        }
        for spec in _SPECS
    ]
