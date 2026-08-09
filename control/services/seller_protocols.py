from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse


class SellerProtocolError(ValueError):
    pass


CRYPTO_TERMS = ("crypto", "coin", "usdc", "erc20", "erc-20", "eth", "bitcoin", "wallet", "onchain", "on-chain")


def rejects_crypto(value: object) -> bool:
    text = str(value or "").strip().casefold()
    return any(term in text for term in CRYPTO_TERMS)


@dataclass(frozen=True)
class PreparedSellerRegistration:
    market: str
    service: dict
    pricing: dict
    mutation_performed: bool = False


class NeverminedFiatContract:
    """Local contract builder only; it never sends a registration or payment request."""

    allowed_interfaces = frozenset({"HTTP", "MCP", "A2A"})
    allowed_price_types = frozenset({"FIXED_FIAT_PRICE"})

    def prepare(self, *, name: str, interface: str, endpoint: str, price: Decimal, currency: str, price_type: str) -> PreparedSellerRegistration:
        interface = interface.upper()
        currency = currency.upper()
        price_type = price_type.upper()
        parsed = urlparse(endpoint)
        if interface not in self.allowed_interfaces:
            raise SellerProtocolError("NEVERMINED_INTERFACE_NOT_SUPPORTED")
        if parsed.scheme != "https" or not parsed.netloc:
            raise SellerProtocolError("SERVICE_ENDPOINT_MUST_BE_HTTPS")
        if currency != "USD" or price_type not in self.allowed_price_types or rejects_crypto(price_type):
            raise SellerProtocolError("NEVERMINED_WEBDOCK_FIAT_ONLY")
        if Decimal(price) <= 0:
            raise SellerProtocolError("SERVICE_PRICE_MUST_BE_POSITIVE")
        return PreparedSellerRegistration(
            market="nevermined",
            service={"name": name, "interface": interface, "agentDefinitionUrl": endpoint},
            pricing={"price": str(price), "currency": currency, "priceType": price_type},
        )


class SkyfireSellerContract:
    """Validates the official seller token boundary without charging or settling it."""

    allowed_settlement_types = frozenset({"CARD", "BANK"})

    def validate_claims(self, claims: dict, *, expected_service_id: str, signature_verified: bool) -> dict:
        if not signature_verified:
            raise SellerProtocolError("SKYFIRE_JWKS_SIGNATURE_NOT_VERIFIED")
        if not isinstance(claims, dict):
            raise SellerProtocolError("SKYFIRE_TOKEN_CLAIMS_INVALID")
        if str(claims.get("ssi") or "") != expected_service_id:
            raise SellerProtocolError("SKYFIRE_SELLER_SERVICE_MISMATCH")
        settlement_type = str(claims.get("stp") or "").upper()
        if settlement_type not in self.allowed_settlement_types:
            raise SellerProtocolError("SKYFIRE_NON_CRYPTO_SETTLEMENT_REQUIRED")
        if str(claims.get("cur") or "").upper() != "USD":
            raise SellerProtocolError("SKYFIRE_USD_SETTLEMENT_REQUIRED")
        if Decimal(str(claims.get("amount") or "0")) <= 0:
            raise SellerProtocolError("SKYFIRE_PAYMENT_AMOUNT_INVALID")
        return {
            "seller_service_id": expected_service_id,
            "settlement_type": settlement_type,
            "currency": "USD",
            "amount": str(claims["amount"]),
            "charge_performed": False,
        }

    def verify_token(self, token: str, *, verifier, expected_service_id: str) -> dict:
        """The injected verifier owns JWKS retrieval/cache policy; this method performs no network I/O."""
        if not token or verifier is None:
            raise SellerProtocolError("SKYFIRE_TOKEN_VERIFIER_REQUIRED")
        claims = verifier(token)
        return self.validate_claims(claims, expected_service_id=expected_service_id, signature_verified=True)


class ExternalSettlementBridge(ABC):
    """Future off-host boundary. No wallet, chain, or transaction implementation belongs on Webdock."""

    @abstractmethod
    def submit_settlement_evidence(self, *, market: str, reference: str, payload: dict) -> dict:
        raise NotImplementedError


class DisabledExternalSettlementBridge(ExternalSettlementBridge):
    def submit_settlement_evidence(self, *, market: str, reference: str, payload: dict) -> dict:
        raise SellerProtocolError("EXTERNAL_SETTLEMENT_BRIDGE_NOT_IMPLEMENTED")


def validate_future_bridge_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise SellerProtocolError("EXTERNAL_SETTLEMENT_BRIDGE_REQUIRES_AUTHENTICATED_HTTPS")
    return url
