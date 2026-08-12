import inspect

from django.test import TestCase

from control.models import GenXModelCatalog
from workers.genx_support import GenXWorkerError, capability_model_ids, research_with_web


class GenXResearchCapabilityTruthTests(TestCase):
    def test_web_search_filter_uses_live_provider_payload_not_all_text_models(self):
        GenXModelCatalog.objects.create(
            model_id="web-capable",
            category="text",
            provider="provider-a",
            active=True,
            model_payload={"capabilities": ["web_search", "reasoning"]},
        )
        GenXModelCatalog.objects.create(
            model_id="plain-text",
            category="text",
            provider="provider-b",
            active=True,
            model_payload={"capabilities": ["reasoning"]},
        )
        self.assertEqual(capability_model_ids("web_search"), ["web-capable"])

    def test_missing_web_search_does_not_silently_broaden_to_every_text_model(self):
        GenXModelCatalog.objects.create(
            model_id="plain-text",
            category="text",
            provider="provider-b",
            active=True,
            model_payload={"capabilities": ["reasoning"]},
        )
        with self.assertRaisesRegex(GenXWorkerError, "web_search"):
            capability_model_ids("web_search")

    def test_research_worker_never_uses_generic_text_fallback_for_web_search(self):
        source = inspect.getsource(research_with_web)
        self.assertIn('eligible = capability_model_ids("web_search")', source)
        self.assertNotIn('fallback_category="text"', source)

    def test_generic_category_fallback_remains_available_for_non_strict_callers(self):
        GenXModelCatalog.objects.create(
            model_id="plain-text",
            category="text",
            provider="provider-b",
            active=True,
            model_payload={"capabilities": ["reasoning"]},
        )
        self.assertEqual(
            capability_model_ids("not-a-capability", fallback_category="text"),
            ["plain-text"],
        )
