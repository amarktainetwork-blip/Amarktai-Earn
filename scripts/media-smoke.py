from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from workers.base import WorkRequest
from workers.media.worker import MediaWorker
from workers.qa.runtime import run_qa


with tempfile.TemporaryDirectory() as value:
    root = Path(value)
    source = root / "source.png"
    Image.new("RGB", (64, 32), "navy").save(source)
    image_result = MediaWorker().execute(WorkRequest(job_id="smoke", workspace=root / "image", inputs={
        "operation": "image_resize", "source": str(source), "width": 32, "height": 16, "output_format": "PNG",
    }))
    assert image_result.ok and run_qa("media", image_result.artifacts[0], image_result.evidence).passed

    wave = root / "source.wav"
    generated = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(wave)],
        timeout=30, check=False,
    )
    assert generated.returncode == 0
    audio_result = MediaWorker().execute(WorkRequest(job_id="smoke", workspace=root / "audio", inputs={
        "operation": "media_transcode", "source": str(wave), "output_format": "mp3",
    }))
    assert audio_result.ok and run_qa("media", audio_result.artifacts[0], audio_result.evidence).passed
