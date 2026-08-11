from __future__ import annotations

import hashlib
import os
from decimal import Decimal
from pathlib import Path

from PIL import Image

from control.models import Job
from gateways.genx.client import GenXError
from gateways.genx.service import GenXGateway
from workers.base import Worker, WorkRequest, WorkResult
from workers.genx_support import GenXWorkerError, credit_envelope, model_parameter_names


class ImageProductWorker(Worker):
    worker_class = "image_product"

    def execute(self, request: WorkRequest) -> WorkResult:
        operation = str(request.inputs.get("operation") or "")
        if operation not in {"image_generate_product_asset", "image_edit_product_asset"}:
            return WorkResult(ok=False, error="unsupported image product operation")
        prompt = " ".join(str(request.inputs.get("prompt") or "").strip().split())
        rights_confirmed = request.inputs.get("rights_safe_original") is True
        if not prompt or not rights_confirmed:
            return WorkResult(ok=False, error="prompt and original rights-safe confirmation are required")
        forbidden = ("celebrity", "copyrighted character", "trademark imitation", "fake testimonial")
        if any(term in prompt.casefold() for term in forbidden):
            return WorkResult(ok=False, error="image concept violates the commercial rights policy")
        width = max(256, min(int(request.inputs.get("width") or 1024), 4096))
        height = max(256, min(int(request.inputs.get("height") or 1024), 4096))
        params = {"prompt": prompt, "width": width, "height": height}
        gateway = GenXGateway()
        estimated, call_limit = credit_envelope(request.job_id, request.inputs)
        job = Job.objects.get(pk=request.job_id)
        required_quality = Decimal(str(request.inputs.get("minimum_quality", "0.85")))
        allow_exploration = bool(request.inputs.get("allow_model_exploration", False))
        economically_fragile = bool(request.inputs.get("economically_fragile", False))
        selected = None
        uploaded_file_id = ""
        if operation == "image_edit_product_asset":
            source = Path(str(request.inputs.get("source") or ""))
            if not source.is_file():
                return WorkResult(ok=False, error="image editing requires a verified source image")
            selected = gateway.select_model(
                task_class="image_editing",
                category="image",
                required_quality=required_quality,
                expected_revenue=job.reward,
                max_genx_cost=call_limit,
                allow_exploration=allow_exploration,
                economically_fragile=economically_fragile,
            )
            uploaded = gateway.client.upload_file(source)
            uploaded_file_id = str(uploaded.get("file_id") or uploaded.get("id") or "")
            uploaded_url = str(uploaded.get("url") or uploaded.get("download_url") or "")
            names = model_parameter_names(selected.model_payload)
            for candidate in ("input_image_file_id", "image_file_id", "input_file_id", "file_id", "asset_id"):
                if candidate in names and uploaded_file_id:
                    params[candidate] = uploaded_file_id
                    break
            else:
                for candidate in ("input_image_url", "image_url", "input_url", "file_url", "url"):
                    if candidate in names and uploaded_url:
                        params[candidate] = uploaded_url
                        break
            if len(params) == 3:
                if uploaded_file_id:
                    try:
                        gateway.client.delete_file(uploaded_file_id)
                    except GenXError:
                        pass
                return WorkResult(ok=False, error="selected image model exposes no recognized source-image input")
        request_key = "image-product:" + hashlib.sha256(
            f"{request.job_id}|{request.attempt}|{operation}|{prompt}|{width}x{height}".encode()
        ).hexdigest()[:48]
        call = None
        try:
            call = gateway.run(
                job_id=request.job_id,
                worker_id=request.worker_id,
                category="image",
                task_class="image_editing" if operation.endswith("edit_product_asset") else "image_generation",
                params=params,
                estimated_credits=estimated,
                max_allowed_credits=call_limit,
                request_key=request_key,
                preferred_model=selected.model_id if selected else None,
                wait_timeout_seconds=int(os.getenv("GENX_IMAGE_TIMEOUT_SECONDS", "420")),
                required_quality=required_quality,
                expected_revenue=job.reward,
                allow_exploration=allow_exploration,
                economically_fragile=economically_fragile,
            )
        finally:
            if uploaded_file_id and call is not None and call.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                try:
                    gateway.client.delete_file(uploaded_file_id)
                except GenXError:
                    # Cleanup failure cannot erase a paid call or its reconciliation truth.
                    pass
        if call.status != "COMPLETED" or not call.external_job_id:
            raise GenXWorkerError(f"GenX image call did not complete: {call.status}")
        raw = gateway.client.job_file(
            call.external_job_id,
            max_bytes=int(os.getenv("GENX_MAX_IMAGE_RESULT_BYTES", "33554432")),
        )
        output = request.workspace / "product-image.png"
        output.write_bytes(raw)
        try:
            with Image.open(output) as image:
                image.load()
                actual_dimensions = list(image.size)
                image_format = str(image.format or "").upper()
        except OSError as exc:
            return WorkResult(ok=False, error=f"GenX image result did not decode: {exc.__class__.__name__}")
        return WorkResult(
            ok=True,
            artifacts=[output],
            evidence={
                "operation": operation,
                "media_kind": "image",
                "max_output_bytes": int(os.getenv("GENX_MAX_IMAGE_RESULT_BYTES", "33554432")),
                "expected_format": image_format,
                "expected_dimensions": actual_dimensions,
                "rights_safe_original": True,
                "genx_call_id": str(call.id),
                "model": call.model,
            },
        )
