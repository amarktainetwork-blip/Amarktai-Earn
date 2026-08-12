from __future__ import annotations

import json
import os
import stat
import subprocess
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

try:
    import resource
except ImportError:  # pragma: no cover - Windows development hosts
    resource = None

from workers.base import WorkRequest, WorkResult, Worker


class MediaWorkerError(RuntimeError):
    pass


IMAGE_OPERATIONS = {"image_resize", "image_center_crop", "image_convert", "image_compress", "image_thumbnail"}
FORMAT_SUFFIX = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _validate_source(source: Path) -> None:
    info = source.lstat()
    if source.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise MediaWorkerError("media source must be a regular non-linked file")
    if info.st_size <= 0 or info.st_size > _int_env("MEDIA_MAX_SOURCE_BYTES", 100 * 1024 * 1024):
        raise MediaWorkerError("media source exceeds configured byte bounds")


def _open_image(source: Path) -> Image.Image:
    Image.MAX_IMAGE_PIXELS = _int_env("MEDIA_MAX_PIXELS", 40_000_000)
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        probe = Image.open(source)
        probe.verify()
        image = Image.open(source)
        image.load()
    width, height = image.size
    if width > _int_env("MEDIA_MAX_DIMENSION", 10000) or height > _int_env("MEDIA_MAX_DIMENSION", 10000) or width * height > Image.MAX_IMAGE_PIXELS:
        image.close()
        raise MediaWorkerError("decoded image exceeds configured dimensions")
    normalized = ImageOps.exif_transpose(image)
    if normalized is not image:
        image.close()
    return normalized


def _bounded_dimension(value, name: str) -> int:
    result = int(value)
    if result < 1 or result > _int_env("MEDIA_MAX_DIMENSION", 10000):
        raise MediaWorkerError(f"{name} is outside configured bounds")
    return result


def _image(request: WorkRequest, source: Path, operation: str) -> WorkResult:
    image = _open_image(source)
    original = image.size
    width = _bounded_dimension(request.inputs.get("width"), "width") if "width" in request.inputs else image.width
    height = _bounded_dimension(request.inputs.get("height"), "height") if "height" in request.inputs else image.height
    output_format = str(request.inputs.get("output_format") or image.format or "PNG").upper().replace("JPG", "JPEG")
    if output_format not in FORMAT_SUFFIX:
        raise MediaWorkerError("requested image format is unsupported")
    quality = int(request.inputs.get("quality", 85))
    if quality < 30 or quality > 95:
        raise MediaWorkerError("image quality must be between 30 and 95")

    if operation == "image_resize":
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    elif operation == "image_center_crop":
        if width > image.width or height > image.height:
            raise MediaWorkerError("crop dimensions exceed decoded source")
        image = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    elif operation == "image_thumbnail":
        image = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
    elif operation not in {"image_convert", "image_compress"}:
        raise MediaWorkerError("unsupported image operation")

    if image.width * image.height > _int_env("MEDIA_MAX_PIXELS", 40_000_000):
        raise MediaWorkerError("output image exceeds pixel limit")
    if output_format == "JPEG" and image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    request.workspace.mkdir(parents=True, exist_ok=True)
    target = request.workspace / ("media-output" + FORMAT_SUFFIX[output_format])
    kwargs = {"format": output_format, "optimize": True}
    if output_format in {"JPEG", "WEBP"}:
        kwargs["quality"] = quality
    image.save(target, **kwargs)
    image.close()
    _check_output(target)
    return WorkResult(ok=True, artifacts=[target], evidence={
        "operation": operation, "media_kind": "image", "source_dimensions": list(original),
        "expected_dimensions": [width, height] if operation in {"image_resize", "image_center_crop", "image_thumbnail"} else list(original),
        "expected_format": output_format, "quality": quality, "output_size_bytes": target.stat().st_size,
        "max_output_bytes": _int_env("MEDIA_MAX_OUTPUT_BYTES", 150 * 1024 * 1024),
    })


def _probe(source: Path) -> dict:
    timeout = _int_env("MEDIA_PROBE_TIMEOUT_SECONDS", 30)
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=index,codec_type,width,height", "-of", "json", str(source)],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if result.returncode != 0:
        raise MediaWorkerError("ffprobe rejected the source media")
    try:
        data = json.loads(result.stdout)
        duration = float(data.get("format", {}).get("duration") or 0)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise MediaWorkerError("media probe response was invalid") from exc
    if duration <= 0 or duration > _int_env("MEDIA_MAX_DURATION_SECONDS", 3600):
        raise MediaWorkerError("media duration is outside configured bounds")
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
            if max(width, height) > _int_env("MEDIA_MAX_VIDEO_DIMENSION", 3840):
                raise MediaWorkerError("video dimensions exceed configured bounds")
    data["duration_seconds"] = duration
    return data


def _check_output(target: Path) -> None:
    if not target.is_file() or target.stat().st_size <= 0 or target.stat().st_size > _int_env("MEDIA_MAX_OUTPUT_BYTES", 150 * 1024 * 1024):
        if target.exists():
            target.unlink()
        raise MediaWorkerError("media output is empty or exceeds configured bounds")


def _remove_output(target: Path) -> None:
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def _ffmpeg(request: WorkRequest, source: Path, operation: str) -> WorkResult:
    probe = _probe(source)
    request.workspace.mkdir(parents=True, exist_ok=True)
    output_format = str(request.inputs.get("output_format") or "").casefold()
    target_suffix = {"mp4": ".mp4", "webm": ".webm", "mp3": ".mp3", "wav": ".wav"}.get(output_format)
    if not target_suffix:
        raise MediaWorkerError("requested media output format is unsupported")
    target = request.workspace / ("media-output" + target_suffix)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    expected_duration = probe["duration_seconds"]
    if operation == "media_trim":
        start, end = float(request.inputs["start_seconds"]), float(request.inputs["end_seconds"])
        if start < 0 or end <= start or end > probe["duration_seconds"]:
            raise MediaWorkerError("trim range is outside source duration")
        expected_duration = end - start
        args += ["-ss", f"{start:.3f}", "-to", f"{end:.3f}"]
    args += ["-i", str(source), "-threads", "1"]
    if operation == "media_extract_audio" or output_format in {"mp3", "wav"}:
        args += ["-vn", "-c:a", "libmp3lame" if output_format == "mp3" else "pcm_s16le"]
    elif output_format == "mp4":
        args += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
    elif output_format == "webm":
        args += ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "6", "-c:a", "libopus"]
    maximum_output = _int_env("MEDIA_MAX_OUTPUT_BYTES", 150 * 1024 * 1024)
    args += ["-fs", str(maximum_output), str(target)]
    run_options = {}
    if resource is not None:
        def impose_file_limit():
            resource.setrlimit(resource.RLIMIT_FSIZE, (maximum_output, maximum_output))
        run_options["preexec_fn"] = impose_file_limit
    try:
        result = subprocess.run(
            args, capture_output=True, text=True,
            timeout=_int_env("MEDIA_PROCESS_TIMEOUT_SECONDS", 600), check=False, **run_options,
        )
        if result.returncode != 0:
            raise MediaWorkerError("ffmpeg transformation failed")
        _check_output(target)
        output_probe = _probe(target)
        if abs(output_probe["duration_seconds"] - expected_duration) > 1.0:
            raise MediaWorkerError("ffmpeg output duration is incomplete")
    except Exception:
        _remove_output(target)
        raise
    return WorkResult(ok=True, artifacts=[target], evidence={
        "operation": operation, "media_kind": "av", "expected_format": output_format,
        "expected_duration_seconds": expected_duration,
        "output_duration_seconds": output_probe["duration_seconds"],
        "require_audio": output_format in {"mp3", "wav"},
        "require_video": output_format in {"mp4", "webm"},
        "max_output_bytes": _int_env("MEDIA_MAX_OUTPUT_BYTES", 150 * 1024 * 1024),
        "output_size_bytes": target.stat().st_size,
    })


def _video_dimensions(probe: dict) -> tuple[int, int] | None:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video":
            return int(stream.get("width") or 0), int(stream.get("height") or 0)
    return None


def _concat_video(request: WorkRequest) -> WorkResult:
    raw_sources = request.inputs.get("sources")
    if not isinstance(raw_sources, list) or not 2 <= len(raw_sources) <= 12:
        raise MediaWorkerError("media_concat requires between 2 and 12 source clips")
    sources = [Path(str(value)) for value in raw_sources]
    probes = []
    for source in sources:
        _validate_source(source)
        probe = _probe(source)
        if not any(stream.get("codec_type") == "video" for stream in probe.get("streams", [])):
            raise MediaWorkerError("every media_concat source must contain video")
        probes.append(probe)
    dimensions = {_video_dimensions(probe) for probe in probes}
    if None in dimensions or len(dimensions) != 1:
        raise MediaWorkerError("media_concat source clips must share the same video dimensions")
    expected_duration = sum(float(probe["duration_seconds"]) for probe in probes)
    if expected_duration > _int_env("MEDIA_MAX_DURATION_SECONDS", 3600):
        raise MediaWorkerError("assembled media duration exceeds configured bounds")
    audio_presence = [any(stream.get("codec_type") == "audio" for stream in probe.get("streams", [])) for probe in probes]
    include_audio = all(audio_presence)

    request.workspace.mkdir(parents=True, exist_ok=True)
    manifest = request.workspace / "concat-inputs.txt"
    lines = []
    for source in sources:
        escaped = str(source.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target = request.workspace / "media-assembled.mp4"
    maximum_output = _int_env("MEDIA_MAX_OUTPUT_BYTES", 150 * 1024 * 1024)
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "concat", "-safe", "0", "-i", str(manifest),
        "-threads", "1", "-c:v", "libx264", "-preset", "veryfast",
    ]
    if include_audio:
        args += ["-c:a", "aac"]
    else:
        args += ["-an"]
    args += ["-fs", str(maximum_output), str(target)]
    run_options = {}
    if resource is not None:
        def impose_file_limit():
            resource.setrlimit(resource.RLIMIT_FSIZE, (maximum_output, maximum_output))
        run_options["preexec_fn"] = impose_file_limit
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_int_env("MEDIA_PROCESS_TIMEOUT_SECONDS", 600),
            check=False,
            **run_options,
        )
        if result.returncode != 0:
            raise MediaWorkerError("ffmpeg video assembly failed")
        _check_output(target)
        output_probe = _probe(target)
        if abs(output_probe["duration_seconds"] - expected_duration) > max(1.0, 0.02 * expected_duration):
            raise MediaWorkerError("assembled video duration is incomplete")
    except Exception:
        _remove_output(target)
        raise
    finally:
        _remove_output(manifest)
    return WorkResult(ok=True, artifacts=[target], evidence={
        "operation": "media_concat",
        "media_kind": "av",
        "expected_format": "mp4",
        "expected_duration_seconds": expected_duration,
        "output_duration_seconds": output_probe["duration_seconds"],
        "require_audio": include_audio,
        "require_video": True,
        "source_count": len(sources),
        "max_output_bytes": maximum_output,
        "output_size_bytes": target.stat().st_size,
    })


class MediaWorker(Worker):
    worker_class = "media"

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            operation = str(request.inputs.get("operation") or "")
            if operation == "media_concat":
                return _concat_video(request)
            source = Path(str(request.inputs["source"]))
            _validate_source(source)
            if operation in IMAGE_OPERATIONS:
                return _image(request, source, operation)
            if operation in {"media_trim", "media_transcode", "media_extract_audio"}:
                return _ffmpeg(request, source, operation)
            return WorkResult(ok=False, error="unsupported media operation")
        except (KeyError, OSError, ValueError, TypeError, subprocess.TimeoutExpired, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, MediaWorkerError) as exc:
            return WorkResult(ok=False, error=str(exc))
