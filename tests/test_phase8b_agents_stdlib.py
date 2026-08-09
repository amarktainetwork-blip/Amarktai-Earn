import json
import tempfile
import unittest
from pathlib import Path

from gateways.genx.client import GenXClient
from gateways.genx.output import (
    decode_text_result_url,
    extract_session_assistant_text,
    extract_session_sources,
    extract_text,
    session_assistant_job_ids,
)
from markets.agentgigs.assets import supported_source_name
from workers.base import WorkRequest
from workers.qa.runtime import run_qa
from workers.registry import operation_spec, registry_manifest
from workers.text_extract import extract_text as extract_local_text


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload

    def close(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class Phase8BAgentContractTests(unittest.TestCase):
    def test_registry_contains_all_four_phase_agents(self):
        manifest = {row["worker_class"]: row for row in registry_manifest()}
        for worker_class in ("documents", "research", "localization", "transcription"):
            self.assertIn(worker_class, manifest)
            self.assertTrue(manifest[worker_class]["requires_genx"])
        self.assertEqual(operation_spec("document_summarize").worker_class, "documents")
        self.assertEqual(operation_spec("research_report").worker_class, "research")
        self.assertEqual(operation_spec("translate_document").worker_class, "localization")
        self.assertEqual(operation_spec("transcribe_media").worker_class, "transcription")

    def test_work_request_carries_persisted_execution_identity(self):
        request = WorkRequest(job_id="job", workspace=Path("/tmp/work"), inputs={}, worker_id="documents-job", execution_id=7, attempt=2)
        self.assertEqual(request.worker_id, "documents-job")
        self.assertEqual(request.execution_id, 7)
        self.assertEqual(request.attempt, 2)

    def test_agentgigs_ingestion_accepts_only_registered_phase_source_types(self):
        for name in ("input.json", "data.csv", "brief.pdf", "brief.docx", "notes.txt", "readme.md", "audio.mp3", "audio.wav", "clip.mp4", "clip.webm"):
            with self.subTest(name=name):
                self.assertTrue(supported_source_name(name))
        for name in ("malware.exe", "archive.zip", "script.sh", "image.png"):
            with self.subTest(name=name):
                self.assertFalse(supported_source_name(name))

    def test_text_extraction_and_new_qa_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.txt"
            source.write_text("A short but valid source document about markets and operations.", encoding="utf-8")
            self.assertIn("valid source", extract_local_text(source))

            document = root / "document.md"
            document.write_text("A concise but valid summary of the supplied source material.", encoding="utf-8")
            outcome = run_qa("document", document, {"operation": "document_summarize", "source_chars": 60})
            self.assertTrue(outcome.passed)

            research = root / "research.md"
            research.write_text(
                "Evidence-based research report with enough detail to be useful. " * 3
                + "\n\nSources\nhttps://example.com/a\nhttps://example.org/b\n",
                encoding="utf-8",
            )
            outcome = run_qa("research", research, {"sources": ["https://example.com/a", "https://example.org/b"]})
            self.assertTrue(outcome.passed)

            translation = root / "translation.md"
            translation.write_text("Este es un documento traducido con contenido suficiente para la validación.", encoding="utf-8")
            outcome = run_qa("translation", translation, {"target_language": "Spanish", "source_chars": 70})
            self.assertTrue(outcome.passed)

            transcript = root / "transcript.txt"
            transcript.write_text("This is a valid transcript.", encoding="utf-8")
            outcome = run_qa("transcript", transcript, {})
            self.assertTrue(outcome.passed)

    def test_genx_output_helpers_extract_text_and_sources(self):
        payload = {
            "choices": [{"message": {"content": "A completed research answer."}}],
            "citations": [{"url": "https://example.com/source"}],
        }
        self.assertEqual(extract_text(payload), "A completed research answer.")
        self.assertEqual(extract_session_sources(payload), ["https://example.com/source"])

    def test_session_output_selects_assistant_for_remote_job_and_decodes_inline_text(self):
        history = {"messages": [
            {"role": "user", "content": [{"type": "text", "text": "Reply exactly OK."}]},
            {"role": "assistant", "job_id": "job-1", "content": [{"type": "text", "text": "OK"}]},
        ]}
        self.assertEqual(extract_session_assistant_text(history, job_id="job-1"), "OK")
        self.assertEqual(session_assistant_job_ids(history), ["job-1"])
        self.assertEqual(decode_text_result_url("data/plain;base64,T0s="), "OK")
        self.assertEqual(decode_text_result_url("data/plain,hello%20world"), "hello world")
        self.assertEqual(decode_text_result_url("data/plain;base64,%%%"), "")
        self.assertEqual(decode_text_result_url("https://example.com/result"), "")

    def test_genx_session_and_upload_use_documented_router_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "brief.txt"
            source.write_text("brief", encoding="utf-8")
            session = FakeSession([
                FakeResponse({"id": "file-1"}),
                FakeResponse({"id": "session-1"}),
                FakeResponse({"message": {"content": "done"}, "usage": {"credits": "0.2"}}),
                FakeResponse({"messages": []}),
                FakeResponse({}),
            ])
            client = GenXClient("gnxk_test", session=session)
            self.assertEqual(client.upload_file(source)["id"], "file-1")
            self.assertEqual(client.create_session("dynamic-model")["id"], "session-1")
            client.session_message("session-1", "research", idempotency_key="k1", tools=[{"type": "web_search"}])
            client.session_messages("session-1")
            client.close_session("session-1")
            paths = [url.split("query.genx.sh", 1)[-1] for _, url, _ in session.calls]
            self.assertEqual(paths, [
                "/api/v1/files",
                "/api/v1/sessions",
                "/api/v1/sessions/session-1/messages",
                "/api/v1/sessions/session-1/messages",
                "/api/v1/sessions/session-1/close",
            ])
            upload_headers = session.calls[0][2]["headers"]
            self.assertNotIn("Content-Type", upload_headers)
            self.assertIn("Authorization", upload_headers)
            self.assertEqual(session.calls[2][2]["json"]["content"], "research")
            self.assertNotIn("message", session.calls[2][2]["json"])
            self.assertEqual(session.calls[2][2]["json"]["tools"], [{"type": "web_search"}])

    def test_planner_contract_contains_all_four_routes_and_safe_blocks(self):
        text = (Path(__file__).resolve().parents[1] / "planning" / "services.py").read_text(encoding="utf-8")
        for operation in ("document_summarize", "document_rewrite", "document_extract_text", "research_report", "translate_document", "transcribe_media"):
            self.assertIn(operation, text)
        self.assertIn("TARGET_LANGUAGE_NOT_EXPLICIT", text)
        self.assertIn("SOURCE_TYPE_NOT_SUPPORTED_BY_REGISTERED_WORKER", text)
        self.assertIn("MULTIPLE_INPUT_ASSETS_AMBIGUOUS", text)


if __name__ == "__main__":
    unittest.main()
