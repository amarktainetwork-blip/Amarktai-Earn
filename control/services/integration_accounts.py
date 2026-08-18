from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from control.models import (
    Alert,
    AuditEvent,
    MarketIntegrationProfile,
    Marketplace,
    MarketplaceCredential,
)
from control.secrets import decrypt_secret, encrypt_secret

SETUP_STATES = (
    "NOT_STARTED",
    "ACCOUNT_OPENING",
    "KYC_REQUIRED",
    "CREDENTIALS_REQUIRED",
    "CREDENTIALS_STORED",
    "CONNECTION_TEST_REQUIRED",
    "CONNECTION_VERIFIED",
    "PAYOUT_CONFIGURATION_REQUIRED",
    "PAYOUT_PROOF_REQUIRED",
    "RECEIPT_ROUTE_VERIFIED",
    "READY_FOR_BOUNDED_LIVE_PROOF",
)


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    required: bool = True
    public_identifier: bool = False
    help_text: str = ""


@dataclass(frozen=True)
class IntegrationDefinition:
    slug: str
    display_name: str
    category: str
    purpose: str
    classification: str
    credential_fields: tuple[CredentialField, ...]
    connection_test: str
    capabilities: tuple[str, ...]
    manual_capabilities: tuple[str, ...]
    payout_route: str
    human_withdrawal_required: bool
    kyc_required: bool = True
    webhook_supported: bool = False
    order_intake: str = "MANUAL_PROOF"
    owner_action: str = "Open the external account and complete its required verification."
    off_host_requirements: tuple[str, ...] = ()


def _field(name: str, label: str, **kwargs) -> CredentialField:
    return CredentialField(name=name, label=label, **kwargs)


DEFINITIONS = (
    IntegrationDefinition("paystack", "Paystack", "DIRECT_COMMERCE", "Owned checkout, payment, and Settlement API receipt", "ACTIVE_PROVING", (_field("secret_key", "Secret key"), _field("public_key", "Public key", required=False, public_identifier=True)), "PAYSTACK", ("CONNECTION_TEST", "DIRECT_CHECKOUT", "WEBHOOKS", "ORDER_INTAKE", "PAYMENT_RECONCILIATION", "SETTLEMENT_RECONCILIATION"), (), "FIAT_SETTLED", False, webhook_supported=True, order_intake="AUTOMATED_AFTER_PROOF", owner_action="Open the merchant account, finish KYC, create test/live keys, configure the webhook, and retain provider-side settlement account configuration in Paystack."),
    IntegrationDefinition("paypal", "PayPal", "TREASURY_RAIL", "Marketplace payout and owner-controlled balance receipt", "CONNECTION_READY", (_field("client_id", "REST app client ID"), _field("client_secret", "REST app client secret")), "PAYPAL", ("CONNECTION_TEST", "RECEIPT_RECONCILIATION"), ("HUMAN_WITHDRAWAL",), "PAYPAL_BALANCE", True, order_intake="NOT_APPLICABLE", owner_action="Create a PayPal REST app with the minimum reporting permissions and prove the owner payout route."),
    IntegrationDefinition("lemon-squeezy", "Lemon Squeezy", "STOREFRONT", "Products, subscriptions, webhooks and merchant orders", "ACTIVE_PROVING", (_field("api_key", "API key"), _field("webhook_secret", "Webhook signing secret")), "LEMON_SQUEEZY", ("CONNECTION_TEST", "STORE_MAPPING", "CHECKOUT", "WEBHOOKS", "ORDER_INTAKE", "REFUNDS", "RECONCILIATION"), ("INITIAL_PUBLICATION",), "PROVIDER_PAYOUT", True, webhook_supported=True, order_intake="AUTOMATED_AFTER_PROOF", owner_action="Complete merchant KYC, add API/webhook credentials, map a store product, and prove the payout receipt route."),
    IntegrationDefinition("payhip", "Payhip", "STOREFRONT", "Digital products and productized services", "INACTIVE", (), "MANUAL", (), ("ACCOUNT_SETUP", "PUBLICATION", "ORDER_PROOF", "PAYOUT_PROOF"), "PAYSTACK_OR_PAYPAL", True, order_intake="MANUAL_PROOF", owner_action="Open the store, configure an owner-usable payout provider externally, then record publication and paid-order proof."),
    IntegrationDefinition("ko-fi", "Ko-fi", "STOREFRONT", "Tips, shop sales, memberships and commissions", "INACTIVE", (), "MANUAL", (), ("ACCOUNT_SETUP", "PUBLICATION", "ORDER_PROOF", "PAYOUT_PROOF"), "PAYPAL", True, order_intake="MANUAL_PROOF", owner_action="Open the creator account, connect PayPal externally, and record non-secret publication/payment proof."),
    IntegrationDefinition("gumroad", "Gumroad", "STOREFRONT", "Digital-product sales", "INACTIVE", (), "MANUAL", (), ("ACCOUNT_SETUP", "PUBLICATION", "ORDER_PROOF", "PAYOUT_PROOF"), "PROVIDER_PAYOUT", True, order_intake="MANUAL_PROOF", owner_action="Complete seller onboarding and record publication and payout proof; no unverified automation is enabled."),
    IntegrationDefinition("patreon", "Patreon", "STOREFRONT", "Membership and recurring creator revenue", "INACTIVE", (), "MANUAL", (), ("ACCOUNT_SETUP", "PUBLICATION", "MEMBERSHIP_PROOF", "PAYOUT_PROOF"), "PAYPAL_OR_PROVIDER_PAYOUT", True, order_intake="MANUAL_PROOF", owner_action="Complete creator onboarding and payout configuration, then record non-secret membership and payout evidence."),
    IntegrationDefinition("rapidapi", "RapidAPI Provider", "API_MARKETPLACE", "API subscriptions and usage revenue", "ACTIVE_PROVING", (_field("proxy_secret", "RapidAPI proxy secret"),), "MANUAL", ("INBOUND_REQUEST_AUTH", "REQUEST_IDEMPOTENCY", "USAGE_TRACKING", "ORDER_INTAKE"), ("PROVIDER_ONBOARDING", "API_PUBLICATION", "PAYOUT_PROOF"), "PAYPAL_BALANCE", True, order_intake="AUTOMATED_AFTER_PUBLICATION", owner_action="Create the provider listing and proxy secret, configure PayPal payout, then record publication and payout proof."),
    IntegrationDefinition("apify-store", "Apify", "OFFHOST_MARKETPLACE", "Paid Actors and off-host execution revenue", "ACTIVE_PROVING", (_field("api_token", "API token"),), "APIFY", ("CONNECTION_TEST", "ACTOR_MAPPING", "RUN_RECONCILIATION", "COST_INGESTION"), ("ACTOR_PUBLICATION", "PAYOUT_PROOF"), "PROVIDER_PAYOUT", True, order_intake="APIFY_OFFHOST", owner_action="Finish creator KYC, add the API token, publish the Actor on Apify, and prove creator payout receipt.", off_host_requirements=("HEAVY_EXECUTION_ON_APIFY",)),
    IntegrationDefinition("taskbounty", "TaskBounty Solver", "BOUNTY_MARKET", "API-native task discovery and submission", "ACTIVE_PROVING", (_field("api_key", "Solver API key"),), "TASKBOUNTY", ("CONNECTION_TEST", "DISCOVERY", "QUALIFICATION", "ACQUISITION", "SUBMISSION", "STATUS"), ("BANK_PAYOUT_ONBOARDING", "PAYOUT_PROOF"), "USD_BANK_TRANSFER", True, order_intake="AUTOMATED_AFTER_PROOF", owner_action="Create the solver key, complete USD bank-transfer onboarding in the TaskBounty dashboard, and prove the bank payout receipt."),
    IntegrationDefinition("contra", "Contra", "PROJECT_MARKET", "Services, projects and payment links", "ACTIVE_PROVING", (), "MANUAL", (), ("ACCOUNT_SETUP", "KYC", "PUBLICATION", "ORDER_INTAKE", "DELIVERY", "PAYOUT_PROOF"), "PAYPAL_OR_PAYONEER", True, order_intake="OWNER_ASSISTED", owner_action="Complete identity and payout setup, publish the canonical packages manually, and use the signed/manual intake boundary."),
    IntegrationDefinition("freelancer", "Freelancer.com", "PROJECT_MARKET", "Official-API project work only after express written automation permission", "OWNER_PERMISSION_REQUIRED", (_field("oauth_token", "OAuth access token"),), "MANUAL", (), ("EXPRESS_WRITTEN_PERMISSION_EVIDENCE", "API_CONTRACT_REVIEW", "PROPOSAL_SUBMISSION", "MESSAGE_HANDLING", "PAYOUT_PROOF"), "PAYPAL_OR_PAYONEER", True, order_intake="BLOCKED", owner_action="Record express written automation/API permission, create the official API application/token, and finish KYC. It is excluded from the autonomous queue until then."),
    IntegrationDefinition("dealwork", "Dealwork", "AGENT_WORK_MARKET", "Agent jobs and bounded platform-wallet proving", "ACTIVE_PROVING", (_field("api_key", "Agent API key"),), "DEALWORK", ("CONNECTION_TEST", "DISCOVERY", "QUALIFICATION", "BID", "CLAIM", "DELIVERY", "WALLET_READ"), ("KYA", "EXTERNAL_WITHDRAWAL_PROOF"), "PLATFORM_WALLET", True, order_intake="AUTOMATED_AFTER_PROOF", owner_action="Finish KYA, add the agent key, and prove the external withdrawal route; wallet balance is not settled cash."),
    IntegrationDefinition("algora", "Algora", "BOUNTY_MARKET", "Coding bounty discovery and delivery", "ACTIVE_PROVING", (), "MANUAL", ("DISCOVERY",), ("APPLICATION", "DELIVERY_PROOF", "PAYOUT_PROOF"), "PROVIDER_PAYOUT", True, order_intake="OWNER_ASSISTED", owner_action="Complete account and payout setup; unsupported acquisition and settlement actions remain manual."),
    IntegrationDefinition("gitpay", "Gitpay", "BOUNTY_MARKET", "Assignment-gated funded Git issue work", "ACTIVE_PROVING", (), "MANUAL", ("DISCOVERY",), ("APPLICATION", "ASSIGNMENT_PROOF", "DELIVERY", "PAYMENT_REQUEST", "PAYOUT_PROOF"), "BANK_OR_PAYPAL", True, order_intake="OWNER_ASSISTED", owner_action="Complete GitHub/account onboarding and bank or PayPal payout setup; record explicit assignment before execution."),
    IntegrationDefinition("nevermined", "Nevermined", "FIAT_API_COMMERCE", "Future fiat agent/API payments", "INACTIVE", (_field("api_key", "API key"),), "MANUAL", (), ("FIAT_PLAN_CONTRACT_REVIEW", "PLAN_PUBLICATION", "PAYMENT_PROOF"), "STRIPE_CONNECT_FIAT_ONLY", True, order_intake="BLOCKED", owner_action="Complete provider onboarding and verify a fiat Stripe Connect plan and payout receipt; all token or on-chain price types remain prohibited."),
    IntegrationDefinition("impact", "impact.com Partner", "AFFILIATE_NETWORK", "Provider-reported affiliate commissions", "INACTIVE", (_field("account_sid", "Account SID"), _field("auth_token", "API auth token")), "MANUAL", (), ("PROGRAM_ENROLMENT", "LINK_PUBLICATION", "COMMISSION_RECONCILIATION", "PAYOUT_PROOF"), "PROVIDER_PAYOUT", True, order_intake="NOT_APPLICABLE", owner_action="Complete partner onboarding and payout setup; only provider-reported commissions may become revenue."),
    IntegrationDefinition("partnerstack", "PartnerStack", "AFFILIATE_NETWORK", "B2B partner and referral commissions", "INACTIVE", (), "MANUAL", (), ("PROGRAM_ENROLMENT", "LINK_PUBLICATION", "COMMISSION_RECONCILIATION", "PAYOUT_PROOF"), "PAYPAL_BALANCE", True, order_intake="NOT_APPLICABLE", owner_action="Complete partner onboarding and PayPal payout; record only provider-authoritative commission evidence."),
    IntegrationDefinition("wise", "Wise", "OPTIONAL_PAYOUT_RAIL", "Optional payout redundancy and FX", "OPTIONAL", (), "MANUAL", (), ("ACCOUNT_SETUP", "PAYOUT_PROOF"), "WISE_BALANCE", True, order_intake="NOT_APPLICABLE", owner_action="Optional: open and verify only when an earning channel supports Wise."),
    IntegrationDefinition("payoneer", "Payoneer", "OPTIONAL_PAYOUT_RAIL", "Optional marketplace payout redundancy", "OPTIONAL", (), "MANUAL", (), ("ACCOUNT_SETUP", "PAYOUT_PROOF"), "PAYONEER_BALANCE", True, order_intake="NOT_APPLICABLE", owner_action="Optional: open and verify for Contra/Freelancer or another supported channel."),
)

BY_SLUG = {definition.slug: definition for definition in DEFINITIONS}

FORBIDDEN_FIELD_TOKENS = ("password", "bank", "account_number", "routing", "totp", "recovery", "seed", "private", "withdrawal_secret", "signing_key")
PRIVATE_MATERIAL_RE = re.compile(r"\b(seed phrase|mnemonic|private key|recovery code|BEGIN (?:EC |RSA )?PRIVATE KEY)\b", re.IGNORECASE)


def _fingerprint(value: str) -> str:
    key = str(settings.SECRET_KEY).encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _safe_definition(definition: IntegrationDefinition) -> dict[str, Any]:
    return {
        "slug": definition.slug,
        "display_name": definition.display_name,
        "category": definition.category,
        "purpose": definition.purpose,
        "classification": definition.classification,
        "credentials_required": bool(definition.credential_fields),
        "credential_fields": [
            {
                "name": field.name,
                "label": field.label,
                "required": field.required,
                "public_identifier": field.public_identifier,
                "help_text": field.help_text,
            }
            for field in definition.credential_fields
        ],
        "connection_test_mode": definition.connection_test,
        "capabilities": list(definition.capabilities),
        "manual_capabilities": list(definition.manual_capabilities),
        "payout_route": definition.payout_route,
        "human_withdrawal_required": definition.human_withdrawal_required,
        "kyc_required": definition.kyc_required,
        "webhook_supported": definition.webhook_supported,
        "order_intake": definition.order_intake,
        "off_host_requirements": list(definition.off_host_requirements),
    }


def _profile_for(slug: str) -> MarketIntegrationProfile | None:
    return MarketIntegrationProfile.objects.select_related("marketplace").filter(marketplace__slug=slug).first()


def _configured_credentials(slug: str) -> dict[str, MarketplaceCredential]:
    return {
        credential.credential_type: credential
        for credential in MarketplaceCredential.objects.filter(marketplace__slug=slug, active=True)
    }


def _computed_setup_state(definition: IntegrationDefinition, profile, configured: dict[str, MarketplaceCredential]) -> str:
    if profile and profile.live_proving_state == "READY":
        return "READY_FOR_BOUNDED_LIVE_PROOF"
    if profile and profile.payout_receipt_proof_state == "VERIFIED":
        return "RECEIPT_ROUTE_VERIFIED"
    if profile and profile.payout_configuration_state not in {"VERIFIED", "NOT_APPLICABLE"}:
        if profile.api_connection_state == "VERIFIED" or not definition.credential_fields:
            return "PAYOUT_CONFIGURATION_REQUIRED"
    if profile and profile.api_connection_state == "VERIFIED":
        return "PAYOUT_PROOF_REQUIRED"
    required = {field.name for field in definition.credential_fields if field.required}
    if required and not required.issubset(configured):
        return "CREDENTIALS_REQUIRED"
    if definition.connection_test != "MANUAL" and required:
        return "CONNECTION_TEST_REQUIRED"
    if definition.kyc_required and (not profile or profile.kyc_state != "VERIFIED"):
        return "KYC_REQUIRED"
    return "ACCOUNT_OPENING" if not profile else profile.setup_state


def integration_account_row(definition: IntegrationDefinition) -> dict[str, Any]:
    profile = _profile_for(definition.slug)
    configured = _configured_credentials(definition.slug)
    row = _safe_definition(definition)
    credential_rows = []
    for field in definition.credential_fields:
        credential = configured.get(field.name)
        credential_rows.append({
            "name": field.name,
            "label": field.label,
            "required": field.required,
            "public_identifier": field.public_identifier,
            "configured": credential is not None,
            "fingerprint": credential.fingerprint if credential else "",
            "updated_at": credential.updated_at.isoformat() if credential else None,
            "verified_at": credential.verified_at.isoformat() if credential and credential.verified_at else None,
            "last_test_at": credential.last_test_at.isoformat() if credential and credential.last_test_at else None,
        })
    row.update({
        "setup_state": _computed_setup_state(definition, profile, configured),
        "credential_state": profile.credential_state if profile else ("NOT_REQUIRED" if not definition.credential_fields else "NOT_CONFIGURED"),
        "kyc_state": profile.kyc_state if profile else ("NOT_APPLICABLE" if not definition.kyc_required else "UNKNOWN"),
        "api_connection_state": profile.api_connection_state if profile else ("MANUAL_PROOF_REQUIRED" if definition.connection_test == "MANUAL" else "UNVERIFIED"),
        "webhook_state": profile.webhook_state if profile else ("UNVERIFIED" if definition.webhook_supported else "NOT_APPLICABLE"),
        "payout_configuration_state": profile.payout_configuration_state if profile else "UNVERIFIED",
        "payout_receipt_proof_state": profile.payout_receipt_proof_state if profile else "UNVERIFIED",
        "work_capability_state": profile.work_capability_state if profile else "UNVERIFIED",
        "live_proving_state": profile.live_proving_state if profile else "BLOCKED",
        "last_connection_status": profile.last_connection_status if profile else "",
        "last_connection_test_at": profile.last_connection_test_at.isoformat() if profile and profile.last_connection_test_at else None,
        "last_connection_success_at": profile.last_connection_success_at.isoformat() if profile and profile.last_connection_success_at else None,
        "last_error_category": profile.last_error_category if profile else "",
        "last_safe_error": profile.last_safe_error if profile else "",
        "last_reconciled_at": profile.last_reconciled_at.isoformat() if profile and profile.last_reconciled_at else None,
        "owner_action_required": (profile.owner_action_required if profile and profile.owner_action_required else definition.owner_action),
        "credentials": credential_rows,
        "connected": bool(profile and profile.api_connection_state == "VERIFIED"),
        "work_ready": bool(profile and profile.work_capability_state == "VERIFIED"),
        "cash_ready": bool(profile and profile.payout_receipt_proof_state == "VERIFIED"),
        "live_entry_ready": bool(profile and profile.live_proving_state == "READY"),
        "autonomy_ready": False,
    })
    return row


def integration_accounts_snapshot() -> dict[str, Any]:
    rows = [integration_account_row(definition) for definition in DEFINITIONS]
    return {
        "rows": rows,
        "meta": {
            "total": len(rows),
            "action_required": sum(1 for row in rows if row["setup_state"] != "READY_FOR_BOUNDED_LIVE_PROOF"),
            "credentials_configured": sum(1 for row in rows if row["credential_state"] in {"CONFIGURED", "VERIFIED"}),
            "connections_verified": sum(1 for row in rows if row["connected"]),
            "work_ready": sum(1 for row in rows if row["work_ready"]),
            "payout_routes_proven": sum(1 for row in rows if row["cash_ready"]),
            "live_entry_ready": sum(1 for row in rows if row["live_entry_ready"]),
            "autonomy_ready": 0,
            "autonomy_state": "OFF",
            "truth": "Credentials, connection, work capability, owner receipt, live proving and autonomy are independent fail-closed gates.",
        },
    }


@transaction.atomic
def ensure_integration_profile(slug: str) -> MarketIntegrationProfile:
    definition = BY_SLUG[slug]
    market, _ = Marketplace.objects.get_or_create(
        slug=slug,
        defaults={"display_name": definition.display_name, "status": Marketplace.Status.WATCH_ONLY, "enabled": False, "payout_ready": False},
    )
    profile, created = MarketIntegrationProfile.objects.get_or_create(
        marketplace=market,
        defaults={
            "adapter_name": f"control.services.integration_connections.{definition.connection_test.lower()}",
            "source_wired": bool(definition.capabilities),
            "manual_onboarding_required": True,
            "category": definition.category,
            "classification": definition.classification,
            "credential_state": "NOT_REQUIRED" if not definition.credential_fields else "NOT_CONFIGURED",
            "api_connection_state": "MANUAL_PROOF_REQUIRED" if definition.connection_test == "MANUAL" else "UNVERIFIED",
            "webhook_state": "UNVERIFIED" if definition.webhook_supported else "NOT_APPLICABLE",
            "owner_action_required": definition.owner_action,
            "off_host_requirements": list(definition.off_host_requirements),
            "capabilities": {name.lower(): True for name in definition.capabilities},
        },
    )
    if created:
        profile.setup_state = "CREDENTIALS_REQUIRED" if definition.credential_fields else "KYC_REQUIRED"
        profile.save(update_fields=["setup_state", "updated_at"])
    return profile


def _validate_credentials(definition: IntegrationDefinition, values: dict[str, Any]) -> dict[str, str]:
    allowed = {field.name: field for field in definition.credential_fields}
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise ValueError(f"unsupported_credential_fields:{','.join(unknown)}")
    cleaned: dict[str, str] = {}
    for name, raw in values.items():
        lower = name.casefold()
        if any(token in lower for token in FORBIDDEN_FIELD_TOKENS):
            raise ValueError("prohibited_credential_type")
        value = str(raw or "").strip()
        if not value:
            continue
        if len(value) > 8192:
            raise ValueError("credential_too_long")
        if PRIVATE_MATERIAL_RE.search(value):
            raise ValueError("private_or_recovery_material_prohibited")
        cleaned[name] = value
    return cleaned


@transaction.atomic
def store_credentials(slug: str, values: dict[str, Any], *, actor: str) -> dict[str, Any]:
    definition = BY_SLUG.get(slug)
    if not definition:
        raise KeyError("unknown_integration")
    if not definition.credential_fields:
        raise ValueError("manual_integration_has_no_credentials")
    cleaned = _validate_credentials(definition, values)
    if not cleaned:
        raise ValueError("no_credentials_supplied")
    profile = ensure_integration_profile(slug)
    now = timezone.now()
    for credential_type, value in cleaned.items():
        MarketplaceCredential.objects.filter(marketplace=profile.marketplace, credential_type=credential_type, active=True).update(active=False, rotated_at=now)
        MarketplaceCredential.objects.create(
            marketplace=profile.marketplace,
            credential_type=credential_type,
            encrypted_value=encrypt_secret(value),
            key_id=settings.FIELD_ENCRYPTION_ACTIVE_KID,
            active=True,
            fingerprint=_fingerprint(value),
        )
    required = {field.name for field in definition.credential_fields if field.required}
    configured = set(_configured_credentials(slug))
    complete = required.issubset(configured)
    profile.credential_state = "CONFIGURED" if complete else "PARTIAL"
    profile.api_connection_state = "UNVERIFIED"
    profile.last_connection_status = ""
    profile.last_error_category = ""
    profile.last_safe_error = ""
    profile.setup_state = "CONNECTION_TEST_REQUIRED" if complete else "CREDENTIALS_REQUIRED"
    profile.live_proving_state = "BLOCKED"
    profile.marketplace.enabled = False
    profile.marketplace.payout_ready = False
    profile.marketplace.save(update_fields=["enabled", "payout_ready", "updated_at"])
    profile.save(update_fields=["credential_state", "api_connection_state", "last_connection_status", "last_error_category", "last_safe_error", "setup_state", "live_proving_state", "updated_at"])
    AuditEvent.objects.create(event_type="integration.credentials_updated", actor=str(actor)[:120], metadata={"integration": slug, "credential_types": sorted(cleaned), "configured": complete})
    return integration_account_row(definition)


def read_credentials(slug: str) -> dict[str, str]:
    return {
        credential.credential_type: decrypt_secret(credential.encrypted_value)
        for credential in MarketplaceCredential.objects.filter(marketplace__slug=slug, active=True)
    }


@transaction.atomic
def revoke_credentials(slug: str, *, actor: str) -> dict[str, Any]:
    definition = BY_SLUG.get(slug)
    if not definition:
        raise KeyError("unknown_integration")
    profile = ensure_integration_profile(slug)
    now = timezone.now()
    revoked = MarketplaceCredential.objects.filter(marketplace=profile.marketplace, active=True).update(active=False, rotated_at=now)
    profile.credential_state = "NOT_CONFIGURED"
    profile.api_connection_state = "REVOKED"
    profile.last_connection_status = "REVOKED"
    profile.last_error_category = "AUTHENTICATION"
    profile.last_safe_error = "Credentials were revoked by the owner."
    profile.work_capability_state = "UNVERIFIED"
    profile.live_proving_state = "BLOCKED"
    profile.setup_state = "CREDENTIALS_REQUIRED"
    profile.autonomous_acquisition_enabled = False
    profile.marketplace.enabled = False
    profile.marketplace.payout_ready = False
    profile.marketplace.status = Marketplace.Status.AUTH_EXPIRED
    profile.marketplace.save(update_fields=["enabled", "payout_ready", "status", "updated_at"])
    profile.save(update_fields=["credential_state", "api_connection_state", "last_connection_status", "last_error_category", "last_safe_error", "work_capability_state", "live_proving_state", "setup_state", "autonomous_acquisition_enabled", "updated_at"])
    Alert.objects.create(severity="WARN", alert_type="INTEGRATION_CREDENTIAL_REVOKED", message=f"{definition.display_name} credentials were revoked; dependent automation is disarmed.", metadata={"integration": slug, "revoked_credentials": revoked})
    AuditEvent.objects.create(severity="WARN", event_type="integration.credentials_revoked", actor=str(actor)[:120], metadata={"integration": slug, "revoked_credentials": revoked, "automation_disarmed": True})
    return integration_account_row(definition)
