from decimal import Decimal
from django.test import SimpleTestCase
from control.economics import EconomicsInput, score_job

class EconomicsTests(SimpleTestCase):
    def test_expected_profit_truth(self):
        r = score_job(EconomicsInput(gross_reward=Decimal("20"), marketplace_fee=Decimal("2"), p_acquire=Decimal("0.5"), p_accept=Decimal("0.8"), p_payment=Decimal("0.9"), expected_genx_cost=Decimal("0.50"), estimated_worker_minutes=Decimal("10")))
        self.assertEqual(r.net_reward, Decimal("18.00"))
        self.assertEqual(r.expected_cash, Decimal("6.48"))
        self.assertEqual(r.expected_profit, Decimal("5.98"))
        self.assertEqual(r.expected_profit_per_minute, Decimal("0.60"))
    def test_claimed_work_acquisition_probability_one(self):
        r = score_job(EconomicsInput(gross_reward=Decimal("10"), marketplace_fee=Decimal("1"), p_acquire=Decimal("1"), p_accept=Decimal("1"), p_payment=Decimal("1"), estimated_worker_minutes=Decimal("3")))
        self.assertEqual(r.expected_profit, Decimal("9.00"))
    def test_rejects_invalid_probability(self):
        with self.assertRaises(ValueError):
            score_job(EconomicsInput(gross_reward=Decimal("10"), marketplace_fee=Decimal("0"), p_acquire=Decimal("1.2"), p_accept=Decimal("1"), p_payment=Decimal("1")))
