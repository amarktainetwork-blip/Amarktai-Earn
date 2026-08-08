from __future__ import annotations

from typing import Callable

from markets.base import MarketAdapter


class SourceWorkflowAdapter(MarketAdapter):
    """Adapter-driven import for official workflows that lack a safe public solver API."""

    def __init__(self, source_reader: Callable[..., list[dict]] | None = None):
        self.source_reader = source_reader

    def health(self):
        return {"ok": self.source_reader is not None, "mode": "SOURCE_WIRED_IMPORT"}

    def payout_status(self):
        return {"ready": False, "reason": "External account and South African payout proof required"}

    def discover_jobs(self, **filters):
        if self.source_reader is None:
            return []
        rows = self.source_reader(**filters)
        return [row for row in rows if isinstance(row, dict)]
