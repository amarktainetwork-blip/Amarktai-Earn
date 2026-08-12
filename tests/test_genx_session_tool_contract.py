from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from control.models import GenXModelCatalog, Job, JobScore, Marketplace, Worker
from workers.genx_support import research_with_web


class GenXSessionToolContractTests(TestCase):
    def setUp(self):
        market = Marketplace.objects.create(slug="session-tools", display_name="Session Tools")
        self.job = Job.objects.create(
            marketplace=market,
            external_id="session-tool-job",
            title="Research session tool proof",
            task_class="Research",
            reward="50.00",
            state=Job.State.AWARDED,
        )
        JobScore.objects.create(
            job=self.job,
            p_acquire="1",
            p_accept="1",
            p_payment="1",
            expected_profit="40",
            expected_minutes=10,
            max_genx_credits="2",
        )
        Worker.objects.create(id="session-tool-worker", worker_class="research", version="1", status="READY")
        GenXModelCatalog.objects.create(
            model_id="alpha-text",
            category="text",
            active=True,
            model_payload={"model": "alpha-text", "provider": "provider-a"},
        )
        GenXModelCatalog.objects.create(
            model_id="beta-text",
            category="text",
            active=True,
            model_payload={"model": "beta-text", "provider": "provider-b"},
        )
        GenXModelCatalog.objects.create(
            model_id="image-only",
            category="image",
            active=True,
            model_payload={"model": "image-only"},
        )

    def test_research_uses_active_text_catalog_when_router_exposes_no_tool_metadata(self):
        captured = {}
        call = SimpleNamespace(
            external_job_id="job-1",
            requested_metadata={"session_id": "session-1", "remote_job_id": "job-1"},
        )
        response = {
            "assistant_text": "Evidence-backed answer.\n\nSources\nhttps://example.com/source",
            "session_history": {
                "messages": [
                    {
                        "role": "assistant",
                        "job_id": "job-1",
                        "content": [{"type": "text", "text": "Evidence-backed answer."}],
                        "citations": [{"url": "https://example.com/source"}],
                    }
                ]
            },
        }

        def run_session(**kwargs):
            captured.update(kwargs)
            return call, response

        gateway = SimpleNamespace(run_session=run_session)
        request = SimpleNamespace(
            job_id=self.job.id,
            worker_id="session-tool-worker",
            attempt=1,
            inputs={},
        )

        with patch("workers.genx_support.GenXGateway", return_value=gateway):
            text, sources, returned_call = research_with_web(request, query="current evidence")

        self.assertIn("Evidence-backed answer", text)
        self.assertEqual(sources, ["https://example.com/source"])
        self.assertIs(returned_call, call)
        self.assertEqual(captured["eligible_model_ids"], ["alpha-text", "beta-text"])
        self.assertEqual(captured["tools"], [{"type": "web_search"}])
