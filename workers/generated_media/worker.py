from __future__ import annotations

import hashlib
import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path

from control.models import GenXModelCatalog, Job
from gateways.genx.client import GenXError
from gateways.genx.service import GenXGateway
from workers.base import WorkRequest, WorkResult, Worker
from workers.genx_support import GenXWorkerError, credit_envelope, model_parameter_names


_OPERATIONS = {
    "voice_generate": {"category": "voice", "task_class": "voice_generation", "keywords": ("voice", "speech", "tts")},
    "audio_generate": {"category": "audio", "task_class": "audio_generation", "keywords": ("audio", "sound", "sfx")},
    "music_generate": {"category": "audio", "task_class": "music_generation", "keywords": ("music", "song", "instrumental")},
    "video_generate": {"category": "video", "task_class": "video_generation", "keywords": ("video",)},
    "image_to_video": {"category": "video", "task_class": "image_to_video", "keywords": ("image", "video")},
}

_SOURCE_IMAGE_FIELDS = {
    "input_image_file_id", "image_file_id", "input_file_id", "file_id", "asset_id",
    "input_image_url", "image_url", "input_url", "file_url", "url",
}


def _catalog_text(row: GenXModelCatalog) -> str:
    try:
        payload = json.dumps(row.model_payload or {}, sort_keys=True, default=str).casefold()
    except (TypeError, ValueError):
        payload = ""
    return f"{row.model_id} {row.provider} {payload}".casefold()


def _eligible_models(category: str, keywords: tuple[str, ...], *, require_source_image: bool = False) -> list[str]:
    rows = list(GenXModelCatalog.objects.filter(active=True, category=category))
    if require_source_image:
        rows = [row for row in rows if model_parameter_names(row.model_payload) & _SOURCE_IMAGE_FIELDS]
    keyword_rows = [row for row in rows if any(keyword in _catalog_text(row) for keyword in keywords)]
    selected = keyword_rows or rows
    return sorted(row.model_id for row in selected)


def _probe_media(path: Path) -> tuple[str, float, list[str]]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,format_name:stream=codec_type",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise GenXWorkerError("generated media did not pass ffprobe")
    try:
        payload = json.loads(result.stdout)
        format_name = str(payload.get("format", {}).get("format_name") or "")
        duration = float(payload.get("format", {}).get("duration") or 0)
        streams = [str(row.get("codec_type") or "") for row in payload.get("streams", [])]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise GenXWorkerError("generated media probe returned invalid metadata") from exc
    if duration <= 0 or not format_name:
        raise GenXWorkerError("generated media probe did not expose duration and format")
    return format_name, duration, streams


def _media_suffix(format_name: str, *, video: bool) -> tuple[str, str]:
    formats = {value.strip().casefold() for value in format_name.split(",") if value.strip()}
    if "webm" in formats:
        return ".webm", "webm"
    if "mp4" in formats or "mov" in formats:
        return (".mp4", "mp4") if video else (".m4a", "mp4")
    if "mp3" in formats:
        return ".mp3", "mp3"
    if "wav" in formats:
        return ".wav", "wav"
    if "ogg" in formats or "oga" in formats:
        return ".ogg", "ogg"
    if "flac" in formats:
        return ".flac", "flac"
    if "aac" in formats:
        return ".aac", "aac"
    if "matroska" in formats:
        return ".mkv", "matroska"
    raise GenXWorkerError(f"generated media format is not in the accepted artifact set: {format_name}")


class GeneratedMediaWorker(Worker):
    worker_class = "generated_media"

    def execute(self, request: WorkRequest) -> WorkResult:
        operation = str(request.inputs.get("operation") or "")
        contract = _OPERATIONS.get(operation)
        if contract is None:
            return WorkResult(ok=False, error="unsupported generated media operation")
        prompt = " ".join(str(request.inputs.get("prompt") or request.inputs.get("text") or "").split())
        if not prompt:
            return WorkResult(ok=False, error="generated media prompt or text is required")
        if request.inputs.get("rights_safe_original") is not True:
            return WorkResult(ok=False, error="original rights-safe generation confirmation is required")
        voice = str(request.inputs.get("voice") or "").strip()
        if operation == "voice_generate" and voice and request.inputs.get("voice_rights_confirmed") is not True:
            return WorkResult(ok=False, error="named or selected voice requires voice-rights confirmation")

        duration = request.inputs.get("duration_seconds")
        if duration is not None:
            try:
                duration = max(1, min(int(duration), int(os.getenv("GENX_MEDIA_MAX_REQUEST_SECONDS", "120"))))
            except (TypeError, ValueError):
                return WorkResult(ok=False, error="duration_seconds must be an integer")

        category = str(contract["category"])
        require_source = operation == "image_to_video"
        eligible = _eligible_models(category, tuple(contract["keywords"]), require_source_image=require_source)
        if not eligible:
            return WorkResult(ok=False, error=f"no active GenX {category} model satisfies the operation contract")

        gateway = GenXGateway()
        estimated, call_limit = credit_envelope(request.job_id, request.inputs)
        job = Job.objects.get(pk=request.job_id)
        params = {"prompt": prompt}
        if duration is not None:
            params["duration_seconds"] = duration
        if voice:
            params["voice"] = voice
        language = str(request.inputs.get("language") or "").strip()
        if language:
            params["language"] = language
        required_quality = Decimal(str(request.inputs.get("minimum_quality", "0.85")))
        selected = gateway.select_model(
            task_class=str(contract["task_class"]),
            category=category,
            eligible_model_ids=eligible,
            required_quality=required_quality,
            expected_revenue=job.reward,
            max_genx_credits=call_limit,
            estimated_credits=estimated,
            params=params,
            accounting_currency=job.currency,
            allow_exploration=bool(request.inputs.get("allow_model_exploration", False)),
            economically_fragile=bool(request.inputs.get("economically_fragile", False)),
            required_params=("prompt",),
        )

        uploaded_file_id = ""
        source_digest = ""
        source_param = ""
        if require_source:
            source = Path(str(request.inputs.get("source") or ""))
            if not source.is_file():
                return WorkResult(ok=False, error="image-to-video requires a verified source image")
            if request.inputs.get("source_authorized") is not True:
                return WorkResult(ok=False, error="image-to-video source authorization is required")
            source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            uploaded = gateway.client.upload_file(source)
            uploaded_file_id = str(uploaded.get("file_id") or uploaded.get("id") or "")
            uploaded_url = str(uploaded.get("url") or uploaded.get("download_url") or "")
            names = model_parameter_names(selected.model_payload)
            for candidate in ("input_image_file_id", "image_file_id", "input_file_id", "file_id", "asset_id"):
                if candidate in names and uploaded_file_id:
                    params[candidate] = uploaded_file_id
                    source_param = candidate
                    break
            if not source_param:
                for candidate in ("input_image_url", "image_url", "input_url", "file_url", "url"):
                    if candidate in names and uploaded_url:
                        params[candidate] = uploaded_url
                        source_param = candidate
                        break
            if not source_param:
                if uploaded_file_id:
                    try:
                        gateway.client.delete_file(uploaded_file_id)
                    except GenXError:
                        pass
                return WorkResult(ok=False, error="selected image-to-video model exposes no usable image input")

        request_key = "generated-media:" + hashlib.sha256(
            f"{request.job_id}|{request.attempt}|{operation}|{prompt}|{duration}|{voice}|{language}|{source_digest}".encode()
        ).hexdigest()[:48]
        call = None
        try:
            required_params = ("prompt", source_param) if source_param else ("prompt",)
            call = gateway.run(
                job_id=request.job_id,
                worker_id=request.worker_id,
                category=category,
                task_class=str(contract["task_class"]),
                params=params,
                estimated_credits=estimated,
                max_allowed_credits=call_limit,
                request_key=request_key,
                eligible_model_ids=[selected.model_id],
                wait_timeout_seconds=int(os.getenv("GENX_GENERATED_MEDIA_TIMEOUT_SECONDS", "900")),
                required_quality=required_quality,
                expected_revenue=job.reward,
                allow_exploration=bool(request.inputs.get("allow_model_exploration", False)),
                economically_fragile=bool(request.inputs.get("economically_fragile", False)),
                required_params=required_params,
            )
            if call.status != "COMPLETED" or not call.external_job_id:
                raise GenXWorkerError(f"GenX generated media call did not complete: {call.status}")
            raw = gateway.client.job_file(
                call.external_job_id,
                max_bytes=int(os.getenv("GENX_MAX_MEDIA_RESULT_BYTES", "268435456")),
            )
        except (GenXError, GenXWorkerError, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))
        finally:
            if uploaded_file_id and call is not None and call.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                try:
                    gateway.client.delete_file(uploaded_file_id)
                except GenXError:
                    pass

        request.workspace.mkdir(parents=True, exist_ok=True)
        provisional = request.workspace / "generated-media.bin"
        provisional.write_bytes(raw)
        try:
            format_name, actual_duration, streams = _probe_media(provisional)
            is_video = operation in {"video_generate", "image_to_video"}
            suffix, expected_format = _media_suffix(format_name, video=is_video)
            target = request.workspace / f"{operation.replace('_', '-')}{suffix}"
            provisional.replace(target)
        except (OSError, subprocess.TimeoutExpired, GenXWorkerError) as exc:
            try:
                provisional.unlink(missing_ok=True)
            except OSError:
                pass
            return WorkResult(ok=False, error=str(exc))

        require_video = operation in {"video_generate", "image_to_video"}
        require_audio = operation in {"voice_generate", "audio_generate", "music_generate"}
        if require_video and "video" not in streams:
            return WorkResult(ok=False, error="generated video artifact contains no video stream")
        if require_audio and "audio" not in streams:
            return WorkResult(ok=False, error="generated audio artifact contains no audio stream")
        return WorkResult(
            ok=True,
            artifacts=[target],
            evidence={
                "operation": operation,
                "media_kind": "av",
                "max_output_bytes": int(os.getenv("GENX_MAX_MEDIA_RESULT_BYTES", "268435456")),
                "expected_format": expected_format,
                "expected_duration_seconds": actual_duration,
                "require_audio": require_audio,
                "require_video": require_video,
                "rights_safe_original": True,
                "source_authorized": request.inputs.get("source_authorized") is True if require_source else None,
                "source_sha256": source_digest,
                "model": call.model,
                "genx_call_id": str(call.id),
            },
        )
