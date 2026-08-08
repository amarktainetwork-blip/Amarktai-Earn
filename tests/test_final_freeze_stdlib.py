from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExpandedV1FreezeContracts(unittest.TestCase):
    def test_readme_is_complete_and_preserves_external_truth(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        markers = (
            "Profit Brain and Growth Governor", "Utilization, micro-profit, opportunity cost, and reputation",
            "Registered worker catalogue", "Multi-file jobs and composite workflows", "GenX architecture",
            "Synthetic Data Factory", "Authorized AI-safety research lane", "Market architecture and exact adapter truth",
            "Dashboard truth", "Security, sandbox, resource governor, and recovery", "External production proof still required",
            "Only `SETTLED`", "NO SCOPE = NO TESTING", "SAFETY_BOUNTY_EXECUTION_ENABLED=0",
        )
        for marker in markers:
            self.assertIn(marker, readme)
        for market in ("AgentGigs", "Dealwork", "Callboard", "TaskBounty", "Opire", "Algora"):
            self.assertIn(market, readme)
        self.assertNotIn("already deployed", readme.casefold())

    def test_acceptance_names_every_expanded_v1_proof_layer(self):
        service = (ROOT / "control/services/v1_acceptance.py").read_text(encoding="utf-8")
        for identifier in (
            "growth_governor", "utilization_economics", "adaptive_economic_learning", "multifile_composite",
            "expanded_worker_qa", "synthetic_data_factory", "authorized_safety_research", "multi_market_adapters",
            "dashboard_economic_truth",
        ):
            self.assertIn(f'"{identifier}"', service)
        self.assertIn("_contract_gate", service)
        self.assertIn("EXTERNAL_PROOF_REQUIRED", service)

    def test_dashboard_source_contains_required_truth_surfaces(self):
        source = (ROOT / "control/ops.py").read_text(encoding="utf-8")
        for marker in (
            "SETTLED 7D", "SETTLED 30D", "AWARDED/ACCEPTED EXPOSURE", "TARGET STATUS",
            "PRODUCTIVE UTILIZATION", "BLOCKED PROFITABLE OPPORTUNITIES 24H", "settlements_total",
            "profit_per_genx_credit", "AVOIDABLE_IDLE", "SYNTHETIC_DATA_QA_DEGRADATION", "SAFETY_SCOPE_EXPIRY",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
