from __future__ import annotations

from pathlib import Path

from workers.base import WorkRequest, WorkResult, Worker
from workers.genx_support import GenXWorkerError, transcribe_media


class TranscriptionWorker(Worker):
    worker_class = "transcription"

    def execute(self, request: WorkRequest) -> WorkResult:
        try:
            if request.inputs.get("operation") != "transcribe_media":
                return WorkResult(ok=False, error="unsupported transcription operation")
            source = Path(request.inputs["source"])
            transcript, call = transcribe_media(request, source)
            request.workspace.mkdir(parents=True, exist_ok=True)
            target = request.workspace / "transcript.txt"
            target.write_text(transcript.strip() + "\n", encoding="utf-8")
            words = len(transcript.split())
            return WorkResult(
                ok=True,
                artifacts=[target],
                evidence={"word_count": words, "output_chars": len(transcript), "model": call.model},
            )
        except (OSError, KeyError, GenXWorkerError, ValueError) as exc:
            return WorkResult(ok=False, error=str(exc))
