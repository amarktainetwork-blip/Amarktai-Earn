from decimal import Decimal
from django.test import SimpleTestCase
from markets.agentgigs.client import AgentGigsAdapter

class AgentGigsNormalizeTests(SimpleTestCase):
    def test_normalizes_cents_to_usd(self):
        a = AgentGigsAdapter("test")
        j = a.normalize_job({"id":"abc","title":"Data cleanup","category":"Data Analysis","budget_min":5000,"budget_max":7500})
        self.assertEqual(j.external_id, "abc")
        self.assertEqual(j.reward, Decimal("75"))
