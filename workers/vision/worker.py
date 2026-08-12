from __future__ import annotations

import hashlib
import os
from decimal import Decimal
from pathlib import Path

from control.models import GenXModelCatalog, Job
from gateways.genx.client import GenXError
from gateways.genx.output import extract_text
from gateways.genx.service import GenXGateway
from workers.base import WorkRequest, WorkResult, Worker
from workers.genx_support import GenXWorkerError, credit_envelope, model_parameter_names


_OPERATIONS = {
    "vision_understand": "Describe and analyze the supplied image faithfully. Identify important visible details and uncertainty; do not infer sensitive traits that are not directly supported.",
    "vision_ocr": "Transcribe all legible text from the supplied image. Preserve reading order where possible and clearly mark uncertain or unreadable text.",
    "vision_qa": "Answer the supplied question using only information visible in the supplied image. State when the image does not support an answer.",
}

_IMAGE_FIELDS = {
    "input_image_file_id", "image_file_id", "input_file_id", "file_id", "asset_id",
    "input_image_url", "image_url", "input_url", "file_url", "url",
}


def _terminal_text(gateway: GenXGateway, call) -> str:
    if call.status != "COMPLETED" or not call.external_job_id:
        raise GenXWorkerError(f"GenX vision call did not complete: {call.status}")
    payload = gateway.client.job(call.external_job_id)
    text = extract_text(payload)
    if not text:
        try:
            text = extract_text(gateway.client.result(call.external_job_id))
        except GenXError:
            text = ""
    if not text:
        try:
            text = gateway.client.job_file(
                call.external_job_id,
                max_bytes=int(os.getenv("GENX_MAX_TEXT_RESULT_BYTES", "8388608")),
            ).decode("utf-8", errors="replace").strip()
        except (GenXError, UnicodeDecodeError):
            text = ""
    if not text:
        raise GenXWorkerError("GenX vision call completed without extractable text")
    return text


class VisionWorker(Worker):
    worker_class = "vision"

    def execute(self, request: WorkRequest) -> WorkResult:
        operation = str(request.inputs.get("operation") or "")
        instruction = _OPERATIONS.get(operation)
        if instruction is None:
            return WorkResult(ok=False, error="unsupported vision operation")
        source = Path(str(request.inputs.get("source") or ""))
        if not source.is_file():
            return WorkResult(ok=False, error="vision operation requires a verified source image")
        if request.inputs.get("source_authorized") is not True:
            return WorkResult(ok=False, error="vision source authorization confirmation is required")
        question = str(request.inputs.get("question") or request.inputs.get("prompt") or "").strip()
        if operation == "vision_qa" and not question:
            return WorkResult(ok=False, error="vision QA requires a question")
        prompt = f"Task: {operation}\nInstruction: {instruction}\nQuestion: {question or '(none)'}"

        gateway = GenXGateway()
        estimated, call_limit = credit_envelope(request.job_id, request.inputs)
        job = Job.objects.get(pk=request.job_id)
        rows = list(GenXModelCatalog.objects.filter(active=True, category="text"))
        eligible = [row.model_id for row in rows if model_parameter_names(row.model_payload) & _IMAGE_FIELDS]
        if not eligible:
            return WorkResult(ok=False, error="no active GenX text model exposes a recognized image input contract")
        required_quality = Decimal(str(request.inputs.get("minimum_quality", "0.85")))
        selected = gateway.select_model(
            task_class=operation,
            category="text",
            eligible_model_ids=eligible,
            required_quality=required_quality,
            expected_revenue=job.reward,
            max_genx_credits=call_limit,
            estimated_credits=estimated,
            params={"prompt": prompt},
            accounting_currency=job.currency,
            allow_exploration=bool(request.inputs.get("allow_model_exploration", False)),
            economically_fragile=bool(request.inputs.get("economically_fragile", False)),
            required_params=("prompt",),
        )
        names = model_parameter_names(selected.model_payload)
        uploaded = gateway.client.upload_file(source)
        file_id = str(uploaded.get("file_id") or uploaded.get("id") or "")
        file_url = str(uploaded.get("url") or uploaded.get("download_url") or "")
        params = {"prompt": prompt}
        source_param = ""
        for candidate in ("input_image_file_id", "image_file_id", "input_file_id", "file_id", "asset_id"):
            if candidate in names and file_id:
                params[candidate] = file_id
                source_param = candidate
                break
        if not source_param:
            for candidate in ("input_image_url", "image_url", "input_url", "file_url", "url"):
                if candidate in names and file_url:
                    params[candidate] = file_url
                    source_param = candidate
                    break
        if not source_param:
            if file_id:
                try:
                    gateway.client.delete_file(file_id)
                except GenXError:
                    pass
            return WorkResult(ok=False, error="selected vision model exposes no usable uploaded-image input")

        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        request_key = f"vision:{operation}:{request.job_id}:{request.attempt}:{digest}"
        call = None
        try:
            call = gateway.run(
                job_id=request.job_id,
                worker_id=request.worker_id,
                category="text",
                task_class=operation,
                params=params,
                estimated_credits=estimated,
                max_allowed_credits=call_limit,
                request_key=request_key,
                eligible_model_ids=[selected.model_id],
                wait_timeout_seconds=int(os.getenv("GENX_VISION_TIMEOUT_SECONDS", "420")),
                required_quality=required_quality,
                expected_revenue=job.reward,
                allow_exploration=bool(request.inputs.get("allow_model_exploration", False)),
                economically_fragile=bool(request.inputs.get("economically_fragile", False)),
                required_params=("prompt", source_param),
            )
            output = _terminal_text(gateway, call)
        except (GenXError, GenXWorkerError, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))
        finally:
            if file_id and call is not None and call.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                try:
                    gateway.client.delete_file(file_id)
                except GenXError:
                    pass

        request.workspace.mkdir(parents=True, exist_ok=True)
        target = request.workspace / f"{operation.replace('_', '-')}.md"
        target.write_text(output.strip() + "\n", encoding="utf-8")
        return WorkResult(
            ok=True,
            artifacts=[target],
            evidence={
                "operation": operation,
                "output_chars": len(output.strip()),
                "source_authorized": True,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "model": call.model,
            },
        )
