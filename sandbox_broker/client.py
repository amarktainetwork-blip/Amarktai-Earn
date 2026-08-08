from __future__ import annotations

import os
from typing import Any

import requests


class SandboxBrokerError(RuntimeError):
    pass


class SandboxBrokerClient:
    def __init__(self, base_url: str | None = None, secret: str | None = None, timeout: int | None = None):
        self.base_url = (base_url or os.getenv("SANDBOX_BROKER_URL", "http://sandbox-broker:8090")).rstrip("/")
        self.secret = secret if secret is not None else os.getenv("SANDBOX_BROKER_SECRET", "")
        self.timeout = timeout or int(os.getenv("SANDBOX_BROKER_TIMEOUT_SECONDS", "1200"))
        if len(self.secret) < 32:
            raise SandboxBrokerError("SANDBOX_BROKER_SECRET must be at least 32 characters")

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(
            self.base_url + "/run",
            headers={"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        if not response.ok:
            raise SandboxBrokerError(f"sandbox broker returned {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise SandboxBrokerError("sandbox broker returned invalid response")
        return data
