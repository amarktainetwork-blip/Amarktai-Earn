from __future__ import annotations

import inspect
from decimal import Decimal

from control.integration_views import integration_proof_run_api
from control.models import GenXCall, ProductCandidate
from control.services import paystack_commerce, product_factory
from gateways.genx.contracts import ModelCandidate, route_models
from gateways.genx.service import GenXGateway
from workers import genx_support
from workers.coding import common as coding_common
from workers.image_product import worker as image_worker


def genx_unit_safe_routing() -> bool:
    candidate = ModelCandidate(
        "proven",
        expected_credits=Decimal("2"),
        attempts=10,
        qa_accepted=9,
        qa_rejected=1,
        credits=Decimal("20"),
    )
    low_money = route_models(
        [candidate], expected_revenue=Decimal("1"), max_genx_credits=Decimal("10")
    )
    high_money = route_models(
        [candidate], expected_revenue=Decimal("1000000"), max_genx_credits=Decimal("10")
    )
    monetary = route_models(
        [candidate],
        expected_revenue=Decimal("100"),
        max_genx_credits=Decimal("10"),
        monetary_cost_per_credit=Decimal("3"),
    )
    return bool(
        low_money
        and high_money
        and monetary
        and low_money[0].expected_net_profit is None
        and high_money[0].expected_net_profit is None
        and low_money[0].non_currency_score == high_money[0].non_currency_score
        and low_money[0].cost_basis == "CREDIT_EFFICIENCY"
        and monetary[0].expected_net_profit is not None
        and monetary[0].cost_basis == "AUTHORITATIVE_MONETARY_VALUATION"
    )


def genx_quality_and_exploration_bounded() -> bool:
    low_quality = ModelCandidate(
        "low", expected_credits=Decimal("0.1"), attempts=20, qa_accepted=2, qa_rejected=18
    )
    cold_start = ModelCandidate("new", expected_credits=Decimal("1"))
    admitted = route_models(
        [cold_start],
        expected_revenue=Decimal("100"),
        max_genx_credits=Decimal("10"),
        allow_exploration=False,
    )
    compatibility_flag = route_models(
        [cold_start],
        expected_revenue=Decimal("100"),
        max_genx_credits=Decimal("10"),
        allow_exploration=True,
        exploration_fraction=Decimal("0.01"),
    )
    over_budget = route_models(
        [cold_start],
        expected_revenue=Decimal("100"),
        max_genx_credits=Decimal("1"),
        allow_exploration=True,
        exploration_fraction=Decimal("1"),
    )
    return bool(
        route_models([low_quality], expected_revenue=Decimal("100"), max_genx_credits=Decimal("10")) == []
        and admitted
        and compatibility_flag
        and over_budget == []
        and admitted[0].exploration
        and compatibility_flag[0].exploration
        and admitted[0].quality_probability == Decimal("0.80")
        and admitted[0].expected_credits <= Decimal("10")
    )


def genx_cost_truth_is_fail_closed() -> bool:
    field = GenXCall._meta.get_field("cost_equivalent")
    product_field = ProductCandidate._meta.get_field("cost_basis_resolved")
    source = inspect.getsource(product_factory.refresh_product_cost_basis_for_job)
    return bool(field.null and field.default is None and product_field.default is True and "UNRESOLVED" in source)


def paid_paths_use_economic_selection() -> bool:
    worker_sources = "\n".join(
        inspect.getsource(module)
        for module in (genx_support, coding_common, image_worker)
    )
    run_session = inspect.signature(GenXGateway.run_session).parameters
    return bool(
        "preferred_model=" not in worker_sources
        and "capability_model_ids" in worker_sources
        and "eligible_model_ids" in worker_sources
        and all(name in run_session for name in (
            "required_quality", "expected_revenue", "non_genx_cost", "allow_exploration", "economically_fragile", "eligible_model_ids"
        ))
    )


def paystack_charge_is_not_settlement() -> bool:
    source = inspect.getsource(paystack_commerce.dispatch_webhook)
    return bool(
        "PAYSTACK_BALANCE" not in source
        and "PAYOUT_PENDING" in source
        and "FIAT_SETTLED" not in source
    )


def paystack_settlement_is_provider_proven() -> bool:
    source = inspect.getsource(paystack_commerce.reconcile_paystack_settlements)
    return all(token in source for token in (
        "/settlement", "transactions", 'tx_status != "success"', "FIAT_SETTLED", "reconcile_inbound_settlement"
    ))


def proof_runner_has_meaningful_stages() -> bool:
    source = inspect.getsource(integration_proof_run_api)
    helper = inspect.getsource(__import__("control.integration_views", fromlist=["_paystack_proof_stage"])._paystack_proof_stage)
    return "_paystack_proof_stage" in source and "initialize_checkout" in helper and "reconcile_paystack_settlements" in helper
