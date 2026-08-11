from __future__ import annotations

import json

from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from control.models import (
    AuditEvent,
    CommercePayment,
    Execution,
    IntegrationProofRun,
    OwnerReceipt,
    ProductCandidate,
    QAResult,
)
from control.services.auth_security import (
    Throttled,
    ensure_not_throttled,
    record_failure,
    reset,
    verify_reauthentication,
)
from control.services.integration_accounts import (
    BY_SLUG,
    ensure_integration_profile,
    integration_account_row,
    integration_accounts_snapshot,
    revoke_credentials,
    store_credentials,
)
from control.services.integration_connections import test_connection
from control.services.paystack_commerce import (
    PaystackCommerceError,
    initialize_checkout,
    reconcile_paystack_settlements,
)
from control.services.product_factory import (
    factory_snapshot,
    product_factory_cycle,
    record_owned_product_publication,
    update_policy,
)


def _json(request):
    try:
        value = json.loads(request.body or b"{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _reauthenticated(request, *, action: str, slug: str):
    owner = getattr(request, "owner", None)
    if not owner:
        return None, None, JsonResponse({"error": "unauthorized"}, status=401)
    data = _json(request)
    password = str(data.pop("password", "") or "")
    code = str(data.pop("code", "") or "")
    subject = str(owner.pk)
    try:
        ensure_not_throttled("reauth_user", subject)
    except Throttled:
        return owner, None, JsonResponse({"error": "reauthentication_failed"}, status=401)
    if not verify_reauthentication(owner, password, code):
        record_failure("reauth_user", subject)
        AuditEvent.objects.create(severity="WARN", event_type=f"integration.{action}_reauth_failed", actor=subject, metadata={"integration": slug})
        return owner, None, JsonResponse({"error": "reauthentication_failed"}, status=401)
    reset("reauth_user", subject)
    return owner, data, None


@require_GET
def integration_accounts_api(request):
    if not getattr(request, "owner", None):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse(integration_accounts_snapshot())


@require_GET
def product_factory_api(request):
    if not getattr(request, "owner", None):
        return JsonResponse({"error": "unauthorized"}, status=401)
    return JsonResponse(factory_snapshot())


@require_POST
def product_factory_policy_api(request):
    owner, data, error = _reauthenticated(request, action="product_factory_policy", slug="owned-products")
    if error:
        return error
    try:
        policy = update_policy(data, actor=str(owner.pk))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "policy": policy})


@require_POST
def product_factory_cycle_api(request):
    owner, _data, error = _reauthenticated(request, action="product_factory_cycle", slug="owned-products")
    if error:
        return error
    result = product_factory_cycle()
    AuditEvent.objects.create(event_type="product_factory.cycle_requested", actor=str(owner.pk)[:120], metadata={"admitted_opportunity_id": result["admitted_opportunity_id"], "paid_execution_started": result["paid_execution_started"]})
    return JsonResponse({"ok": True, "result": result})


@require_POST
def product_factory_publication_api(request, product_slug):
    owner, data, error = _reauthenticated(request, action="product_factory_publication", slug=product_slug)
    if error:
        return error
    try:
        product = record_owned_product_publication(
            product_slug=product_slug,
            channel=str(data.get("channel") or "").strip(),
            remote_listing_id=str(data.get("remote_listing_id") or ""),
            remote_reference=str(data.get("remote_reference") or ""),
            actor=str(owner.pk),
        )
    except ProductCandidate.DoesNotExist:
        return JsonResponse({"error": "product_not_found"}, status=404)
    except (KeyError, ValueError) as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({"ok": True, "product": product.slug, "state": product.state, "published_at": product.published_at.isoformat() if product.published_at else None})


@require_http_methods(["POST", "DELETE"])
def integration_credentials_api(request, slug):
    owner, data, error = _reauthenticated(request, action="credentials", slug=slug)
    if error:
        return error
    try:
        if request.method == "DELETE":
            row = revoke_credentials(slug, actor=str(owner.pk))
        else:
            credentials = data.get("credentials")
            if not isinstance(credentials, dict):
                return JsonResponse({"error": "credentials_object_required"}, status=400)
            row = store_credentials(slug, credentials, actor=str(owner.pk))
    except KeyError:
        return JsonResponse({"error": "unknown_integration"}, status=404)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True, "account": row})


@require_POST
def integration_connection_test_api(request, slug):
    owner, _data, error = _reauthenticated(request, action="connection_test", slug=slug)
    if error:
        return error
    try:
        result = test_connection(slug, actor=str(owner.pk))
    except KeyError:
        return JsonResponse({"error": "unknown_integration"}, status=404)
    return JsonResponse({"ok": result.ok, "result": result.public(), "account": integration_account_row(BY_SLUG[slug])}, status=200 if result.ok else 409)


@require_POST
def integration_proof_submission_api(request, slug):
    owner, data, error = _reauthenticated(request, action="proof_submission", slug=slug)
    if error:
        return error
    if slug not in BY_SLUG:
        return JsonResponse({"error": "unknown_integration"}, status=404)
    proof_type = str(data.get("proof_type") or "").strip().upper()
    reference = " ".join(str(data.get("proof_reference") or "").strip().split())[:255]
    if proof_type not in {"ACCOUNT", "KYC", "PUBLICATION", "PAYOUT_CONFIGURATION", "PAYOUT_RECEIPT"}:
        return JsonResponse({"error": "invalid_proof_type"}, status=400)
    if not reference:
        return JsonResponse({"error": "proof_reference_required"}, status=400)
    profile = ensure_integration_profile(slug)
    evidence = dict(profile.evidence or {})
    submissions = dict(evidence.get("owner_proof_submissions") or {})
    submissions[proof_type] = {"reference": reference, "status": "SUBMITTED_FOR_AUTHORITATIVE_VERIFICATION"}
    evidence["owner_proof_submissions"] = submissions
    profile.evidence = evidence
    if proof_type == "KYC":
        profile.kyc_state = "SUBMITTED"
    elif proof_type == "PAYOUT_CONFIGURATION":
        profile.payout_configuration_state = "SUBMITTED"
    elif proof_type == "PAYOUT_RECEIPT":
        profile.payout_receipt_proof_state = "SUBMITTED"
    profile.live_proving_state = "BLOCKED"
    profile.save(update_fields=["evidence", "kyc_state", "payout_configuration_state", "payout_receipt_proof_state", "live_proving_state", "updated_at"])
    AuditEvent.objects.create(event_type="integration.owner_proof_submitted", actor=str(owner.pk)[:120], metadata={"integration": slug, "proof_type": proof_type, "reference_present": True, "authoritative": False})
    return JsonResponse({"ok": True, "status": "SUBMITTED_FOR_AUTHORITATIVE_VERIFICATION", "account": integration_account_row(BY_SLUG[slug])})


@require_POST
def integration_proof_run_api(request, slug):
    owner, data, error = _reauthenticated(request, action="proof_run", slug=slug)
    if error:
        return error
    if slug not in BY_SLUG:
        return JsonResponse({"error": "unknown_integration"}, status=404)
    stage = str(data.get("stage") or "").strip().upper()
    stages = ("TEST_CONNECTION", "TEST_READ", "TEST_PERMITTED_WRITE", "PROVE_PUBLICATION", "PROVE_PAID_ORDER", "PROVE_EXECUTION", "PROVE_QA", "PROVE_DELIVERY", "PROVE_PAYOUT_RECEIPT", "PROVE_RECONCILIATION")
    if stage not in stages:
        return JsonResponse({"error": "invalid_proof_stage"}, status=400)
    market = ensure_integration_profile(slug).marketplace
    if stage in {"TEST_CONNECTION", "TEST_READ"}:
        result = test_connection(slug, actor=str(owner.pk))
        state = IntegrationProofRun.State.PASSED if result.ok and result.authoritative else IntegrationProofRun.State.BLOCKED
        authoritative = bool(result.ok and result.authoritative)
        detail = result.safe_message or "Enter valid provider credentials, then rerun the authoritative read-only test."
    elif slug == "paystack":
        state, authoritative, detail = _paystack_proof_stage(stage, data)
    else:
        state = IntegrationProofRun.State.BLOCKED
        authoritative = False
        profile = ensure_integration_profile(slug)
        submissions = dict((profile.evidence or {}).get("owner_proof_submissions") or {})
        proof_by_stage = {
            "PROVE_PUBLICATION": "PUBLICATION",
            "PROVE_PAYOUT_RECEIPT": "PAYOUT_RECEIPT",
        }
        submitted = submissions.get(proof_by_stage.get(stage, ""))
        if submitted:
            detail = "Owner evidence is submitted but remains non-authoritative until the provider-backed reconciliation verifies it."
        else:
            detail = {
                "TEST_PERMITTED_WRITE": "Complete the provider's explicitly bounded test/sandbox action, then submit its reference; no production spend was attempted.",
                "PROVE_PUBLICATION": "Publish the approved listing externally and submit its provider URL or ID for verification.",
                "PROVE_PAID_ORDER": "Complete one bounded provider test order and wait for authoritative provider evidence.",
                "PROVE_EXECUTION": "Complete the bounded provider-side execution, then rerun this stage against persisted evidence.",
                "PROVE_QA": "Complete independent QA for the persisted test execution, then rerun this stage.",
                "PROVE_DELIVERY": "Complete the bounded delivery through the provider and supply its remote delivery reference.",
                "PROVE_PAYOUT_RECEIPT": "Wait for the provider payout and submit its provider receipt reference for reconciliation.",
                "PROVE_RECONCILIATION": "Run the integration reconciler after provider evidence exists; no success is simulated.",
            }.get(stage, "This stage requires authoritative provider evidence.")
    proof = IntegrationProofRun.objects.create(marketplace=market, stage=stage, state=state, authoritative=authoritative, safe_detail=detail[:500], performed_by=str(owner.pk)[:120])
    return JsonResponse({"ok": state == IntegrationProofRun.State.PASSED, "proof": {"id": proof.pk, "stage": proof.stage, "state": proof.state, "authoritative": proof.authoritative, "safe_detail": proof.safe_detail}}, status=200 if state == IntegrationProofRun.State.PASSED else 409)


def _paystack_proof_stage(stage: str, data: dict) -> tuple[str, bool, str]:
    passed = IntegrationProofRun.State.PASSED
    blocked = IntegrationProofRun.State.BLOCKED
    try:
        if stage == "TEST_PERMITTED_WRITE":
            required = ("offering_slug", "customer_email", "idempotency_key")
            if any(not str(data.get(key) or "").strip() for key in required):
                return blocked, False, "Choose a test offering and provide customer_email plus idempotency_key; only a Paystack TEST-key checkout will be initialized."
            payment = initialize_checkout(
                offering_slug=str(data["offering_slug"]),
                customer_email=str(data["customer_email"]),
                idempotency_key=str(data["idempotency_key"]),
                proof_mode=True,
            )
            return passed, True, f"Bounded TEST checkout initialized as {payment.external_reference}; complete it in Paystack test mode."
        payment = CommercePayment.objects.filter(provider="paystack", evidence__proof_mode=True).order_by("-created_at").first()
        if stage == "PROVE_PAID_ORDER":
            if payment and payment.state == CommercePayment.State.PAID and payment.authoritative:
                return passed, True, "Authoritative Paystack test charge evidence funded the canonical inbound order; settlement remains pending."
            return blocked, False, "Complete the initialized Paystack TEST checkout and wait for its signed charge.success webhook."
        if stage == "PROVE_EXECUTION":
            execution = Execution.objects.filter(job_id=payment.order.job_id).order_by("-created_at").first() if payment and payment.order_id else None
            if execution and execution.status in {"QA_PASSED", "NEEDS_REPAIR", "COMPLETED"}:
                return passed, True, f"Canonical execution {execution.id} reached {execution.status}."
            return blocked, False, "Run the funded test order through the canonical worker execution path."
        if stage == "PROVE_QA":
            qa = QAResult.objects.filter(job_id=payment.order.job_id).order_by("-created_at").first() if payment and payment.order_id else None
            if qa:
                return (passed, True, f"Independent QA evidence recorded: passed={qa.passed}.") if qa.passed else (blocked, True, "Independent QA rejected the test output; repair remains bounded.")
            return blocked, False, "Complete independent QA for the Paystack-funded test execution."
        if stage == "PROVE_DELIVERY":
            if payment and payment.order_id and payment.order.status in {"DELIVERED", "PAYOUT_PENDING", "SETTLED"}:
                return passed, True, f"Canonical order delivery lifecycle reached {payment.order.status}."
            return blocked, False, "Deliver the QA-accepted test artifact through the canonical order lifecycle."
        if stage in {"PROVE_PAYOUT_RECEIPT", "PROVE_RECONCILIATION"}:
            reconciliation = reconcile_paystack_settlements(limit=25)
            receipt = OwnerReceipt.objects.filter(
                marketplace__slug="paystack",
                state=OwnerReceipt.State.FIAT_SETTLED,
                authoritative=True,
            ).order_by("-observed_at").first()
            if receipt:
                return passed, True, "Paystack Settlement API evidence mapped an exact transaction and created one authoritative FIAT_SETTLED receipt."
            if reconciliation.get("test_proofs") or CommercePayment.objects.filter(provider="paystack", evidence__test_settlement_proven=True).exists():
                return passed, True, "Paystack TEST settlement evidence reconciled successfully; it remains non-cash and created no production receipt."
            return blocked, False, "Wait for a successful Paystack settlement containing the exact test transaction, then rerun reconciliation."
        if stage == "PROVE_PUBLICATION":
            return blocked, False, "Paystack Direct has no publication mutation in this proof stage; initialize the bounded TEST checkout next."
    except PaystackCommerceError as exc:
        return blocked, False, exc.safe_message
    return blocked, False, "The requested Paystack proof stage has no safe automatic action."
