from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from workers.base import WorkRequest
from workers.generated_media.worker import GeneratedMediaWorker, _media_suffix
from workers.intelligence.worker import IntelligenceWorker
from workers.qa.runtime import run_qa
from workers.research.worker import ResearchWorker
from workers.structured_semantic.worker import StructuredSemanticWorker
from workers.vision.worker import VisionWorker


INTELLIGENCE_OPERATIONS = (
    "intelligence_chat",
    "intelligence_reason",
    "intelligence_qa",
    "intelligence_summarize",
    "intelligence_rewrite",
    "intelligence_analyze",
)
STRUCTURED_OPERATIONS = (
    "structured_json_generate",
    "classify_text",
    "extract_structured_facts",
)
RESEARCH_OPERATIONS = (
    "research_report",
    "competitor_research",
    "market_research",
    "website_research",
    "multi_source_research",
    "fact_extraction_research",
)
VISION_OPERATIONS = ("vision_understand", "vision_ocr", "vision_qa")
GENERATED_MEDIA_OPERATIONS = (
    "voice_generate",
    "audio_generate",
    "music_generate",
    "video_generate",
    "image_to_video",
)


def request(root: Path, operation: str, **inputs) -> WorkRequest:
    return WorkRequest(
        job_id="00000000-0000-0000-0000-000000000001",
        workspace=root / operation,
        inputs={"operation": operation, **inputs},
        worker_id="phase2-fixture-worker",
        execution_id=1,
        attempt=1,
    )


class Phase2TextCapabilityFixtureTests(SimpleTestCase):
    def test_general_intelligence_operations_materialize_and_pass_structural_qa(self):
        with tempfile.TemporaryDirectory() as temp, patch(
            "workers.intelligence.worker.generate_text",
            side_effect=lambda req, *, prompt, task_class: (
                f"Fixture answer for {task_class}. This output contains enough words for independent structural QA.",
                SimpleNamespace(model="fixture-text-model"),
            ),
        ):
            root = Path(temp)
            for operation in INTELLIGENCE_OPERATIONS:
                with self.subTest(operation=operation):
                    result = IntelligenceWorker().execute(
                        request(root, operation, prompt="Analyze the supplied fixture text.", context="Fixture context")
                    )
                    self.assertTrue(result.ok, result.error)
                    self.assertEqual(len(result.artifacts), 1)
                    qa = run_qa("transcript", result.artifacts[0], result.evidence)
                    self.assertTrue(qa.passed)
                    self.assertEqual(result.evidence["operation"], operation)
                    self.assertEqual(result.evidence["model"], "fixture-text-model")

    def test_structured_semantic_operations_parse_validate_and_reopen_json(self):
        outputs = {
            "structured_json_generate": '{"name":"Amarktai","ready":true}',
            "classify_text": '{"label":"positive","confidence":0.99}',
            "extract_structured_facts": '```json\n{"facts":[{"name":"price","value":42}]}\n```',
        }
        with tempfile.TemporaryDirectory() as temp, patch(
            "workers.structured_semantic.worker.generate_text",
            side_effect=lambda req, *, prompt, task_class: (
                outputs[task_class], SimpleNamespace(model="fixture-json-model")
            ),
        ):
            root = Path(temp)
            for operation in STRUCTURED_OPERATIONS:
                with self.subTest(operation=operation):
                    result = StructuredSemanticWorker().execute(
                        request(root, operation, text="Fixture source with only supported facts.")
                    )
                    self.assertTrue(result.ok, result.error)
                    payload = json.loads(result.artifacts[0].read_text(encoding="utf-8"))
                    self.assertTrue(payload["valid"])
                    self.assertEqual(payload["errors"], [])
                    self.assertEqual(payload["operation"], operation)
                    qa = run_qa("tabular", result.artifacts[0], result.evidence)
                    self.assertTrue(qa.passed)

    def test_all_research_modes_use_cited_web_research_and_reopen_qa(self):
        report = (
            "# Fixture Research\n\n"
            "This fixture report contains substantive evidence and enough text to satisfy the deterministic research gate. "
            "It cites https://example.com/source-one and independently cites https://example.org/source-two.\n\n"
            "## Sources\n- https://example.com/source-one\n- https://example.org/source-two\n"
        )
        with tempfile.TemporaryDirectory() as temp, patch(
            "workers.research.worker.research_with_web",
            return_value=(
                report,
                ["https://example.com/source-one", "https://example.org/source-two"],
                SimpleNamespace(model="fixture-research-model"),
            ),
        ) as mocked:
            root = Path(temp)
            for operation in RESEARCH_OPERATIONS:
                with self.subTest(operation=operation):
                    result = ResearchWorker().execute(request(root, operation, query="Fixture market"))
                    self.assertTrue(result.ok, result.error)
                    self.assertEqual(result.evidence["operation"], operation)
                    self.assertTrue(run_qa("research", result.artifacts[0], result.evidence).passed)
            self.assertEqual(mocked.call_count, len(RESEARCH_OPERATIONS))


class Phase2VisionAndMediaFixtureTests(SimpleTestCase):
    def test_visual_operations_use_uploaded_authorized_source_and_one_dynamic_model(self):
        row = SimpleNamespace(
            model_id="fixture-vision-model",
            provider="fixture",
            model_payload={"parameters": [{"name": "prompt"}, {"name": "image_file_id"}]},
        )
        gateway = Mock()
        gateway.select_model.return_value = row
        gateway.client.upload_file.return_value = {"file_id": "fixture-file"}
        gateway.run.return_value = SimpleNamespace(
            status="COMPLETED", external_job_id="fixture-job", model="fixture-vision-model", id="call-vision"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.png"
            source.write_bytes(b"fixture-image-bytes")
            with patch("workers.vision.worker.GenXModelCatalog.objects.filter", return_value=[row]), patch(
                "workers.vision.worker.GenXGateway", return_value=gateway
            ), patch(
                "workers.vision.worker.credit_envelope", return_value=(Decimal("1"), Decimal("100"))
            ), patch(
                "workers.vision.worker.Job.objects.get", return_value=SimpleNamespace(reward=Decimal("10"), currency="USD")
            ), patch(
                "workers.vision.worker._terminal_text",
                side_effect=lambda gateway, call: "Fixture visual answer contains enough words for structural QA.",
            ):
                for operation in VISION_OPERATIONS:
                    with self.subTest(operation=operation):
                        inputs = {"source": str(source), "source_authorized": True}
                        if operation == "vision_qa":
                            inputs["question"] = "What is visible?"
                        result = VisionWorker().execute(request(root, operation, **inputs))
                        self.assertTrue(result.ok, result.error)
                        self.assertTrue(run_qa("transcript", result.artifacts[0], result.evidence).passed)
                        self.assertEqual(result.evidence["operation"], operation)
                self.assertEqual(gateway.run.call_count, len(VISION_OPERATIONS))
                for call in gateway.run.call_args_list:
                    params = call.kwargs["params"]
                    self.assertEqual(params["image_file_id"], "fixture-file")

    def test_generated_media_operations_are_dynamic_bounded_and_probe_before_artifact(self):
        selected = SimpleNamespace(
            model_id="fixture-media-model",
            model_payload={"parameters": [
                {"name": "prompt"}, {"name": "text"}, {"name": "duration"}, {"name": "image_file_id"}
            ]},
        )
        gateway = Mock()
        gateway.select_model.return_value = selected
        gateway.client.upload_file.return_value = {"file_id": "fixture-image-file"}
        gateway.client.job_file.return_value = b"fixture-provider-media"
        gateway.run.return_value = SimpleNamespace(
            status="COMPLETED", external_job_id="fixture-media-job", model="fixture-media-model", id="call-media"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.png"
            source.write_bytes(b"fixture-source-image")
            with patch("workers.generated_media.worker._eligible_models", return_value=["fixture-media-model"]), patch(
                "workers.generated_media.worker.GenXGateway", return_value=gateway
            ), patch(
                "workers.generated_media.worker.credit_envelope", return_value=(Decimal("1"), Decimal("100"))
            ), patch(
                "workers.generated_media.worker.Job.objects.get", return_value=SimpleNamespace(reward=Decimal("20"), currency="USD")
            ):
                for operation in GENERATED_MEDIA_OPERATIONS:
                    with self.subTest(operation=operation), patch(
                        "workers.generated_media.worker._probe_media",
                        return_value=(
                            ("mov,mp4,m4a,3gp,3g2,mj2", 5.0, ["video"])
                            if operation in {"video_generate", "image_to_video"}
                            else ("mp3", 5.0, ["audio"])
                        ),
                    ):
                        inputs = {
                            "prompt": "Original fixture media concept",
                            "rights_safe_original": True,
                            "duration_seconds": 5,
                        }
                        if operation == "image_to_video":
                            inputs.update({"source": str(source), "source_authorized": True})
                        result = GeneratedMediaWorker().execute(request(root, operation, **inputs))
                        self.assertTrue(result.ok, result.error)
                        self.assertTrue(result.artifacts[0].is_file())
                        self.assertEqual(result.evidence["operation"], operation)
                        self.assertEqual(result.evidence["model"], "fixture-media-model")
                        if operation in {"video_generate", "image_to_video"}:
                            self.assertTrue(result.evidence["require_video"])
                        else:
                            self.assertTrue(result.evidence["require_audio"])

                voice_select = gateway.select_model.call_args_list[0]
                self.assertEqual(voice_select.kwargs["params"]["text"], "Original fixture media concept")
                self.assertNotIn("prompt", voice_select.kwargs["params"])
                self.assertEqual(voice_select.kwargs["required_params"], ("text",))
                voice_run = gateway.run.call_args_list[0]
                self.assertEqual(voice_run.kwargs["params"]["text"], "Original fixture media concept")
                self.assertEqual(voice_run.kwargs["required_params"], ("text",))

    def test_generated_media_rejects_unconfirmed_rights_before_provider_boundary(self):
        with tempfile.TemporaryDirectory() as temp, patch("workers.generated_media.worker.GenXGateway") as gateway:
            result = GeneratedMediaWorker().execute(
                request(Path(temp), "video_generate", prompt="fixture", rights_safe_original=False)
            )
            self.assertFalse(result.ok)
            gateway.assert_not_called()

    def test_media_container_mapping_is_deterministic(self):
        self.assertEqual(_media_suffix("mov,mp4,m4a,3gp,3g2,mj2", video=True), (".mp4", "mp4"))
        self.assertEqual(_media_suffix("mov,mp4,m4a,3gp,3g2,mj2", video=False), (".m4a", "mp4"))
        self.assertEqual(_media_suffix("mp3", video=False), (".mp3", "mp3"))
        self.assertEqual(_media_suffix("matroska,webm", video=True), (".webm", "webm"))
