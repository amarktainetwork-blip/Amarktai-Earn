import unittest
from decimal import Decimal
from control.economics import EconomicsInput, score_job

class EconomicsStdlibTests(unittest.TestCase):
    def test_expected_profit(self):
        r = score_job(EconomicsInput(gross_reward=Decimal("20"), marketplace_fee=Decimal("2"), p_acquire=Decimal("0.5"), p_accept=Decimal("0.8"), p_payment=Decimal("0.9"), expected_genx_cost=Decimal("0.50"), estimated_worker_minutes=Decimal("10")))
        self.assertEqual(r.expected_cash, Decimal("6.48"))
        self.assertEqual(r.expected_profit, Decimal("5.98"))

if __name__ == "__main__":
    unittest.main()
