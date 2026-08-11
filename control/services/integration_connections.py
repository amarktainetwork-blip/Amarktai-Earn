from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import asdict, dataclass
from typing import Any

import requests
from django.db import transaction
from django.utils import timezone

from control.models import Alert, AuditEvent, MarketIntegrationProfile, MarketplaceCredential
from control.services.integration_accounts import (
    BY_SLUG,
    ensure_integration_profile,
    read_credentials,
)

DEFAULT_TIMEOUT_SECONDS = 12


@dataclass(frozen=True)
class IntegrationConnectionResult:
    ok: bool
    authoritative: bool
    account_id: str | None
    account_label: str | None
    capabilities: tuple[str, ...]
    error_code: str | None
    safe_message: str | None
    checked_at: str

    def public(self) -> dict[str, Any]:
        return asdict(self)


class ConnectionTestError(RuntimeError):
    def __init__(self, code: str, safe_message: str):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def _request(session, method: str, url: str, **kwargs):
    try:
        response = session.request(method, url, timeout=DEFAULT_TIMEOUT_SECONDS, **kwargs)
    except requests.Timeout as exc:
        raise ConnectionTestError("TIMEOUT", "The provider connection test timed out.") from exc
    except requests.RequestException as exc:
        raise ConnectionTestError("NETWORK", "The provider connection test could not reach the provider.") from exc
    if response.status_code in {401, 403}:
        raise ConnectionTestError("AUTHENTICATION", "The provider rejected the configured credentials.")
    if response.status_code == 429:
        raise ConnectionTestError("RATE_LIMIT", "The provider rate-limited the connection test; credentials remain unverified.")
    if not 200 <= response.status_code < 300:
        raise ConnectionTestError("PROVIDER_REJECTION", f"The provider rejected the read-only connection test (HTTP {response.status_code}).")
    try:
        return response.json()
    except ValueError as exc:
        raise ConnectionTestError("MALFORMED_RESPONSE", "The provider returned a malformed response.") from exc


def _required(credentials: dict[str, str], *names: str) -> None:
    if any(not credentials.get(name) for name in names):
        raise ConnectionTestError("CREDENTIALS_MISSING", "Required credentials have not been configured.")


def _paystack(credentials: dict[str, str], session) -> tuple[str | None, str | None, tuple[str, ...]]:
    _required(credentials, "secret_key")
    payload = _request(
        session,
        "GET",
        "https://api.paystack.co/transaction",
        headers={"Authorization": f"Bearer {credentials['secret_key']}", "Accept": "application/json"},
        params={"perPage": 1, "page": 1},
    )
    if not isinstance(payload, dict) or payload.get("status") is not True or not isinstance(payload.get("data"), list):
        raise ConnectionTestError("MALFORMED_RESPONSE", "Paystack returned an unexpected transaction-list response.")
    return None, "Paystack merchant integration", ("TRANSACTION_READ", "DIRECT_CHECKOUT", "SIGNED_WEBHOOKS")


def _paypal(credentials: dict[str, str], session) -> tuple[str | None, str | None, tuple[str, ...]]:
    _required(credentials, "client_id", "client_secret")
    payload = _request(
        session,
        "POST",
        "https://api-m.paypal.com/v1/oauth2/token",
        auth=(credentials["client_id"], credentials["client_secret"]),
        headers={"Accept": "application/json"},
        data={"grant_type": "client_credentials"},
    )
    if not isinstance(payload, dict) or not payload.get("access_token") or payload.get("token_type", "").casefold() != "bearer":
        raise ConnectionTestError("MALFORMED_RESPONSE", "PayPal returned an unexpected OAuth response.")
    return str(payload.get("app_id") or "") or None, "PayPal REST application", ("OAUTH", "RECEIPT_RECONCILIATION")


def _valr(credentials: dict[str, str], session) -> tuple[str | None, str | None, tuple[str, ...]]:
    _required(credentials, "api_key", "view_api_secret")
    timestamp = str(int(time.time() * 1000))
    path = "/v1/account/balances"
    message = f"{timestamp}GET{path}"
    signature = hmac.new(credentials["view_api_secret"].encode(), message.encode(), hashlib.sha512).hexdigest()
    payload = _request(
        session,
        "GET",
        f"https://api.valr.com{path}",
        headers={"X-VALR-API-KEY": credentials["api_key"], "X-VALR-SIGNATURE": signature, "X-VALR-TIMESTAMP": timestamp, "Accept": "application/json"},
    )
    if not isinstance(payload, list):
        raise ConnectionTestError("MALFORMED_RESPONSE", "VALR returned an unexpected balances response.")
    return None, "VALR view-only account", ("BALANCE_READ", "RECEIPT_RECONCILIATION")


def _lemon_squeezy(credentials: dict[str, str], session) -> tuple[str | None, str | None, tuple[str, ...]]:
    _required(credentials, "api_key")
    payload = _request(
        session,
        "GET",
        "https://api.lemonsqueezy.com/v1/users/me",
        headers={"Authorization": f"Bearer {credentials['api_key']}", "Accept": "application/vnd.api+json", "Content-Type": "application/vnd.api+json"},
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("type") != "users" or not data.get("id"):
        raise ConnectionTestError("MALFORMED_RESPONSE", "Lemon Squeezy returned an unexpected user response.")
    attributes = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    return str(data["id"]), str(attributes.get("name") or "Lemon Squeezy merchant"), ("ACCOUNT_READ", "STORE_MAPPING", "SIGNED_WEBHOOKS")


def _apify(credentials: dict[str, str], session) -> tuple[str | None, str | None, tuple[str, ...]]:
    _required(credentials, "api_token")
    payload = _request(
        session,
        "GET",
        "https://api.apify.com/v2/users/me",
        headers={"Authorization": f"Bearer {credentials['api_token']}", "Accept": "application/json"},
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not data.get("id"):
        raise ConnectionTestError("MALFORMED_RESPONSE", "Apify returned an unexpected user response.")
    return str(data["id"]), str(data.get("username") or "Apify creator"), ("ACCOUNT_READ", "ACTOR_RUN_RECONCILIATION", "COST_INGESTION")


def _taskbounty(credentials: dict[str, str], session) -> tuple[str | None, str | None, tuple[str, ...]]:
    _required(credentials, "api_key")
    payload = _request(
        session,
        "GET",
        "https://www.task-bounty.com/api/v1/tasks",
        headers={"Authorization": f"Bearer {credentials['api_key']}", "Accept": "application/json"},
        params={"state": "open", "limit": 1},
    )
    if not isinstance(payload, (dict, list)):
        raise ConnectionTestError("MALFORMED_RESPONSE", "TaskBounty returned an unexpected task response.")
    return None, "TaskBounty solver", ("DISCOVERY", "SUBMISSION", "STATUS")


def _dealwork(credentials: dict[str, str], session) -> tuple[str | None, str | None, tuple[str, ...]]:
    _required(credentials, "api_key")
    payload = _request(
        session,
        "GET",
        "https://dealwork.ai/api/v1/jobs",
        headers={"Authorization": f"Bearer {credentials['api_key']}", "Accept": "application/json"},
        params={"per_page": 1, "sort": "newest"},
    )
    if not isinstance(payload, (dict, list)):
        raise ConnectionTestError("MALFORMED_RESPONSE", "Dealwork returned an unexpected jobs response.")
    return None, "Dealwork agent", ("DISCOVERY", "BID", "CLAIM", "DELIVERY", "WALLET_READ")


TESTERS = {
    "PAYSTACK": _paystack,
    "PAYPAL": _paypal,
    "VALR": _valr,
    "LEMON_SQUEEZY": _lemon_squeezy,
    "APIFY": _apify,
    "TASKBOUNTY": _taskbounty,
    "DEALWORK": _dealwork,
}


def _result(*, ok: bool, authoritative: bool, account_id=None, account_label=None, capabilities=(), error_code=None, safe_message=None):
    return IntegrationConnectionResult(ok, authoritative, account_id, account_label, tuple(capabilities), error_code, safe_message, timezone.now().isoformat())


def test_connection(slug: str, *, actor: str, session=None) -> IntegrationConnectionResult:
    definition = BY_SLUG.get(slug)
    if not definition:
        raise KeyError("unknown_integration")
    ensure_integration_profile(slug)
    if definition.connection_test == "MANUAL":
        result = _result(ok=False, authoritative=False, error_code="MANUAL_PROOF_REQUIRED", safe_message="This integration has no enabled authoritative read-only connection test; use the documented manual proof boundary.")
    else:
        tester = TESTERS[definition.connection_test]
        try:
            account_id, account_label, capabilities = tester(read_credentials(slug), session or requests.Session())
            result = _result(ok=True, authoritative=True, account_id=account_id, account_label=account_label, capabilities=capabilities, safe_message="Authoritative read-only connection test passed.")
        except ConnectionTestError as exc:
            result = _result(ok=False, authoritative=False, error_code=exc.code, safe_message=exc.safe_message)
        except Exception:  # noqa: BLE001 - provider adapters must fail closed without leaking details
            result = _result(ok=False, authoritative=False, error_code="INTERNAL", safe_message="The connection test failed safely before verification.")

    return _persist_connection_result(slug=slug, definition=definition, result=result, actor=actor)


@transaction.atomic
def _persist_connection_result(*, slug, definition, result, actor) -> IntegrationConnectionResult:
    """Persist only after remote I/O completes; never hold locks across the API call."""
    profile = MarketIntegrationProfile.objects.select_for_update().select_related("marketplace").get(marketplace__slug=slug)
    now = timezone.now()
    profile.last_connection_test_at = now
    profile.last_connection_status = "VERIFIED" if result.ok and result.authoritative else (result.error_code or "FAILED")
    profile.last_error_category = "" if result.ok else (result.error_code or "UNKNOWN")
    profile.last_safe_error = "" if result.ok else (result.safe_message or "Connection test failed.")[:300]
    profile.api_connection_state = "VERIFIED" if result.ok and result.authoritative else ("MANUAL_PROOF_REQUIRED" if result.error_code == "MANUAL_PROOF_REQUIRED" else "UNVERIFIED")
    if result.ok and result.authoritative:
        profile.last_connection_success_at = now
        profile.credential_state = "VERIFIED"
        profile.setup_state = "PAYOUT_CONFIGURATION_REQUIRED"
        MarketplaceCredential.objects.filter(marketplace=profile.marketplace, active=True).update(verified_at=now, last_test_at=now)
    else:
        MarketplaceCredential.objects.filter(marketplace=profile.marketplace, active=True).update(last_test_at=now)
        profile.live_proving_state = "BLOCKED"
        profile.autonomous_acquisition_enabled = False
        profile.marketplace.enabled = False
        profile.marketplace.payout_ready = False
        profile.marketplace.save(update_fields=["enabled", "payout_ready", "updated_at"])
        if result.error_code == "AUTHENTICATION":
            Alert.objects.create(severity="WARN", alert_type="INTEGRATION_AUTHENTICATION_FAILED", message=f"{definition.display_name} rejected the configured credentials; dependent automation remains disarmed.", metadata={"integration": slug})
    profile.save(update_fields=["last_connection_test_at", "last_connection_status", "last_error_category", "last_safe_error", "api_connection_state", "last_connection_success_at", "credential_state", "setup_state", "live_proving_state", "autonomous_acquisition_enabled", "updated_at"])
    AuditEvent.objects.create(event_type="integration.connection_tested", actor=str(actor)[:120], metadata={"integration": slug, "ok": result.ok, "authoritative": result.authoritative, "error_code": result.error_code, "capabilities": list(result.capabilities)})
    return result
