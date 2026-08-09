import unittest
from decimal import Decimal

from gateways.genx.contracts import (
    ModelCandidate,
    assert_credit_budget,
    available_credits,
    effective_reserved_credits,
    price_hint,
    pricing_index,
    rank_models,
    records,
    result_url,
    usage_credits,
)


class GenXContractTests(unittest.TestCase):
    def test_records_support_common_api_shapes(self):
        self.assertEqual(records([{"id": "root-dict"}, "root-string"]), [{"id": "root-dict"}, {"id": "root-string"}])
        self.assertEqual(records({"models": [{"id": "m1"}]}), [{"id": "m1"}])
        self.assertEqual(records({"data": {"m2": {"category": "text"}}}), [{"category": "text", "id": "m2"}])

    def test_records_normalize_string_model_ids(self):
        self.assertEqual(
            records({"data": ["text-model-a", "image-model-b"]}),
            [{"id": "text-model-a"}, {"id": "image-model-b"}],
        )

    def test_records_ignore_unsupported_list_scalars(self):
        self.assertEqual(
            records({"data": [None, False, 0, 1.5, [], "", "   ", {"id": "dict-model"}, " string-model "]}),
            [{"id": "dict-model"}, {"id": "string-model"}],
        )

    def test_pricing_and_price_hint_are_shape_tolerant(self):
        payload = {"pricing": {"m1": {"input_credits_per_million": "12.5", "output_credits_per_million": "30"}}}
        indexed = pricing_index(payload)
        self.assertIn("m1", indexed)
        self.assertEqual(price_hint(indexed["m1"]), Decimal("12.5"))

    def test_pricing_index_supports_structured_data_rows(self):
        payload = {
            "data": [
                {
                    "model": "text-model-a",
                    "category": "text",
                    "provider": "provider-a",
                    "pricing": {"credits": 1},
                }
            ]
        }
        indexed = pricing_index(payload)
        self.assertEqual(indexed["text-model-a"], payload["data"][0])
        self.assertEqual(price_hint(indexed["text-model-a"]), Decimal("1"))

    def test_credits_and_result_usage_are_extracted_without_inventing_cost(self):
        self.assertEqual(available_credits({"wallet": {"available_credits": "912.25"}}), Decimal("912.25"))
        self.assertEqual(usage_credits({"status": "completed", "usage": {"credits": "3.75"}}), Decimal("3.75"))
        self.assertEqual(result_url({"result": {"url": "https://example.test/result"}}), "https://example.test/result")
        self.assertIsNone(usage_credits({"status": "completed", "usage": {"tokens": 42}}))

    def test_ranking_prefers_proven_profit_per_credit_then_acceptance(self):
        candidates = [
            ModelCandidate("cheap-unproven", price_hint=Decimal("1")),
            ModelCandidate("proven-a", price_hint=Decimal("10"), attempts=10, accepted=8, profit=Decimal("20"), credits=Decimal("10")),
            ModelCandidate("proven-b", price_hint=Decimal("2"), attempts=10, accepted=9, profit=Decimal("15"), credits=Decimal("10")),
        ]
        self.assertEqual(rank_models(candidates)[0].model_id, "proven-a")
        self.assertEqual(rank_models([candidates[0], ModelCandidate("other", price_hint=Decimal("2"))])[0].model_id, "cheap-unproven")
        negative = ModelCandidate("known-loss", price_hint=Decimal("0.1"), attempts=5, accepted=5, profit=Decimal("-1"), credits=Decimal("5"))
        self.assertEqual(rank_models([negative, candidates[0]])[0].model_id, "cheap-unproven")

    def test_budget_reservation_counts_estimates_until_actual_usage_exists(self):
        reserved = effective_reserved_credits([
            (Decimal("0"), Decimal("2.5"), "SUBMITTED"),
            (Decimal("1.2"), Decimal("2"), "COMPLETED"),
            (Decimal("0"), Decimal("99"), "FAILED"),
        ])
        self.assertEqual(reserved, Decimal("3.7"))
        self.assertEqual(
            effective_reserved_credits([
                (Decimal("0"), Decimal("2.5"), "COMPLETED", {"billing_truth": "UNRESOLVED"}),
                (Decimal("0"), Decimal("9"), "COMPLETED", {"billing_truth": "ACTUAL"}),
            ]),
            Decimal("2.5"),
        )
        assert_credit_budget(
            already_reserved=reserved,
            estimated=Decimal("1.0"),
            call_limit=Decimal("1.5"),
            job_limit=Decimal("5"),
        )
        with self.assertRaisesRegex(ValueError, "remaining job budget"):
            assert_credit_budget(
                already_reserved=reserved,
                estimated=Decimal("1.5"),
                call_limit=Decimal("2"),
                job_limit=Decimal("5"),
            )


if __name__ == "__main__":
    unittest.main()
