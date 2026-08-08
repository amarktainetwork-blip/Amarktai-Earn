from __future__ import annotations

from typing import Any

import requests


class MarketHTTPError(RuntimeError):
    def __init__(self, market: str, status_code: int | None = None, retry_after: int | None = None):
        super().__init__(f"{market} request failed" + (f" with HTTP {status_code}" if status_code else ""))
        self.status_code = status_code
        self.retry_after = retry_after


class JSONHTTPClient:
    """Small secret-safe JSON transport shared by official REST adapters."""

    def __init__(self, *, market: str, base_url: str, headers: dict[str, str], timeout: int = 20, session=None):
        self.market = market
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.timeout = timeout
        self.session = session or requests.Session()

    def request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        try:
            response = self.session.request(
                method, self.base_url + path, headers=self.headers, timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise MarketHTTPError(self.market) from exc
        if not response.ok:
            retry_after = None
            try:
                retry_after = int(response.headers.get("Retry-After", ""))
            except (TypeError, ValueError, AttributeError):
                pass
            raise MarketHTTPError(self.market, response.status_code, retry_after)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketHTTPError(self.market, response.status_code) from exc
        return payload if isinstance(payload, dict) else {"data": payload}
