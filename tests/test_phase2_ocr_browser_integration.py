from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from workers.base import WorkRequest
from workers.ocr.worker import OCRWorker
from workers.qa.runtime import run_qa


class Phase2OCRCapabilityTests(SimpleTestCase):
    def test_ocr_document_image_materializes_text_and_reopens_qa(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "scan.png"
            source.write_bytes(b"fixture-scan")
            request = WorkRequest(
                job_id="fixture-job",
                workspace=root / "workspace",
                inputs={"operation": "ocr_document", "source": str(source), "language": "eng"},
                worker_id="fixture-ocr",
                execution_id=1,
                attempt=1,
            )
            with patch(
                "workers.ocr.worker._tesseract",
                return_value="Fixture scanned document contains enough words for independent OCR structural verification.",
            ):
                result = OCRWorker().execute(request)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.evidence["runtime"], "tesseract+poppler")
            self.assertEqual(result.evidence["page_count"], 1)
            self.assertTrue(run_qa("transcript", result.artifacts[0], result.evidence).passed)

    def test_ocr_document_rejects_unsupported_language_before_process_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "scan.png"
            source.write_bytes(b"fixture")
            request = WorkRequest(
                job_id="fixture-job",
                workspace=Path(temp) / "workspace",
                inputs={"operation": "ocr_document", "source": str(source), "language": "fra"},
                worker_id="fixture-ocr",
                execution_id=1,
                attempt=1,
            )
            with patch("workers.ocr.worker.subprocess.run") as process:
                result = OCRWorker().execute(request)
            self.assertFalse(result.ok)
            process.assert_not_called()


class Phase2ApifyBrowserContractTests(SimpleTestCase):
    def test_actor_source_declares_authorized_browser_modes_and_never_submits_generic_forms(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "integrations" / "apify_actor" / "src" / "main.py").read_text(encoding="utf-8")
        requirements = (root / "integrations" / "apify_actor" / "requirements.txt").read_text(encoding="utf-8")
        dockerfile = (root / "integrations" / "apify_actor" / ".actor" / "Dockerfile").read_text(encoding="utf-8")
        schema = (root / "integrations" / "apify_actor" / ".actor" / "input_schema.json").read_text(encoding="utf-8")

        for capability in (
            "browser_snapshot",
            "browser_extract",
            "form_inspect",
            "form_fill_preview",
        ):
            self.assertIn(capability, source)
            self.assertIn(capability, schema)
        self.assertIn("authorization_confirmed", source)
        self.assertIn('"form_submitted": False', source)
        self.assertIn("playwright", requirements.casefold())
        self.assertIn("playwright install --with-deps chromium", dockerfile)
        self.assertIn("compileall", dockerfile)
        self.assertNotIn("form.submit(", source)
        self.assertNotIn("locator.press(\"Enter\")", source)
