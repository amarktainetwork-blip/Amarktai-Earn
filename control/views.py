import json
from datetime import timedelta

import jwt
import pyotp
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .jwt_auth import issue_access, issue_refresh, revoke_refresh, rotate_refresh
from .models import (
    AuditEvent,
    GenXAccountSnapshot,
    GenXCall,
    Job,
    LoginChallenge,
    OwnerSecurityProfile,
    Payout,
    ReauthenticationGrant,
    RecoveryCode,
    RefreshSession,
    Worker,
)
from .ops import SECTIONS
from .ops import snapshot as ops_snapshot
from .secrets import decrypt_secret
from .services.auth_security import (
    Throttled,
    client_ip,
    consume_reauthentication_grant,
    ensure_not_throttled,
    issue_reauthentication_grant,
    record_failure,
    reset,
    verify_reauthentication,
)
from .services.autonomy import current_mode

User = get_user_model()

PAGE_SECTIONS = (*SECTIONS, "jobs", "money", "system", "capabilities", "services", "channels", "audit", "commercial", "autonomous-earn")
PAGE_META = {
    "overview": ("Overview", "Your autonomous earning business at a glance"),
    "jobs": ("Jobs", "Live work, delivery progress, and payout state"),
    "live-work": ("Jobs", "Live work, delivery progress, and payout state"),
    "agents": ("Agents", "Your digital workforce and runtime readiness"),
    "capabilities": ("Operations & Capabilities", "Registry-derived operation contracts, proof requirements, and launch status"),
    "money": ("Earnings", "Settled cash, pending payouts, costs, and ledger truth"),
    "earnings": ("Payouts", "Detailed earnings and settlement lifecycle"),
    "treasury": ("Treasury", "Balances and the advanced accounting ledger"),
    "markets": ("Markets & Accounts", "Accounts, connections, work capability, payout proof, and market readiness"),
    "channels": ("Channels", "Marketplace connection, acquisition, service, payout, and revenue truth"),
    "accounts": ("Markets & Accounts", "Canonical integration-account readiness"),
    "alerts": ("Alerts", "Meaningful owner attention and resolved events"),
    "system": ("System Overview", "Infrastructure, AI runtime, and operating health"),
    "genx": ("AI / GenX", "Model usage, credits, and call reconciliation"),
    "nodes": ("Infrastructure", "Controller, worker nodes, and service heartbeats"),
    "storage": ("Storage", "Capacity, persistent paths, and resource admission"),
    "performance": ("Performance", "Execution quality, utilization, and growth evidence"),
    "logs": ("Audit Logs", "Technical events and correlation evidence"),
    "audit": ("Audit", "Owner-readable events with filterable technical evidence"),
    "services": ("Services & Products", "Canonical offerings, listings, orders, delivery, and settlement state"),
    "commercial": ("Commercial", "Sellable inventory, API economics, customers, experiments, and settled outcomes"),
    "autonomous-earn": ("Autonomous Earn", "Existing paid demand, expected settled profit, work state, and payout truth"),
    "security": ("Security", "Owner access, sessions, and protected secret state"),
    "settings": ("Settings", "Connections, limits, AI, security, system health, and audit evidence"),
}


def _json(request):
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return {}


def _set_auth_cookies(response, access, refresh):
    common = dict(secure=settings.SESSION_COOKIE_SECURE, httponly=True, samesite="Strict", path="/")
    response.set_cookie(settings.ACCESS_COOKIE_NAME, access, max_age=settings.JWT_ACCESS_SECONDS, **common)
    response.set_cookie(settings.REFRESH_COOKIE_NAME, refresh, max_age=settings.JWT_REFRESH_SECONDS, **common)


def _clear_auth_cookies(response):
    response.delete_cookie(settings.ACCESS_COOKIE_NAME, path="/", samesite="Strict")
    response.delete_cookie(settings.REFRESH_COOKIE_NAME, path="/", samesite="Strict")
    response.delete_cookie(settings.PREAUTH_COOKIE_NAME, path="/", samesite="Strict")
    response.delete_cookie("amarktai_reauth", path="/", samesite="Strict")

@require_GET
def healthz(request):
    return JsonResponse({"status": "ok", "service": "amarktai-earn"})

@ensure_csrf_cookie
@require_GET
def csrf_cookie(request):
    return JsonResponse({"csrf": "ready"})

@ensure_csrf_cookie
def login_page(request):
    if getattr(request, "owner", None):
        return redirect("overview")
    return render(request, "control/login.html")

def overview_page(request):
    return ops_page(request, "overview")


def markets_accounts_page(request):
    return ops_page(request, "markets")


def treasury_page(request):
    return ops_page(request, "treasury")


def ops_page(request, section="overview"):
    if not getattr(request, "owner", None):
        return redirect("login")
    if section not in PAGE_SECTIONS:
        return JsonResponse({"error": "unknown_section"}, status=404)
    consolidated = {"system", "nodes", "storage", "performance", "security"}
    if section in consolidated:
        return redirect(f"/ops/settings/?view={section}")
    if section == "logs":
        return redirect("/ops/audit/")
    title, description = PAGE_META.get(section, (section.replace("-", " ").title(), "Owner operations"))
    return render(request, "control/operations.html", {
        "section": section,
        "page_title": title,
        "page_description": description,
        "advanced_section": False,
    })


@require_GET
def ops_api(request, section):
    if not getattr(request, "owner", None):
        return JsonResponse({"error": "unauthorized"}, status=401)
    if section == "commercial":
        from control.services.commercial_intelligence import commercial_snapshot
        return JsonResponse(commercial_snapshot())
    if section == "autonomous-earn":
        from control.services.autonomous_income import autonomous_earn_snapshot
        return JsonResponse(autonomous_earn_snapshot())
    try:
        return JsonResponse(ops_snapshot(section, owner=request.owner))
    except KeyError:
        return JsonResponse({"error": "unknown_section"}, status=404)

@require_POST
def password_login(request):
    data = _json(request)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    ip = client_ip(request)
    try:
        ensure_not_throttled("password_ip", f"{username}|{ip}")
        ensure_not_throttled("password_user", username)
    except Throttled:
        AuditEvent.objects.create(severity="WARN", event_type="auth.password_throttled", actor=username[:120])
        return JsonResponse({"error": "authentication_failed"}, status=401)
    user = authenticate(request, username=username, password=password)
    if not user or not user.is_active or not user.is_staff:
        record_failure("password_ip", f"{username}|{ip}")
        record_failure("password_user", username)
        AuditEvent.objects.create(severity="WARN", event_type="auth.password_failed", actor=username[:120])
        return JsonResponse({"error": "authentication_failed"}, status=401)
    reset("password_ip", f"{username}|{ip}")
    profile, _ = OwnerSecurityProfile.objects.get_or_create(user=user)
    if not profile.totp_secret_encrypted or not profile.totp_confirmed_at:
        return JsonResponse({"error": "authentication_failed"}, status=401)
    challenge = LoginChallenge.objects.create(user=user, expires_at=timezone.now() + timedelta(minutes=5))
    response = JsonResponse({"requires_2fa": True})
    response.set_cookie(settings.PREAUTH_COOKIE_NAME, str(challenge.id), max_age=300, secure=settings.SESSION_COOKIE_SECURE, httponly=True, samesite="Strict", path="/")
    return response

@require_POST
def verify_totp(request):
    challenge_id = request.COOKIES.get(settings.PREAUTH_COOKIE_NAME)
    if not challenge_id:
        return JsonResponse({"error": "authentication_failed"}, status=401)
    with transaction.atomic():
        challenge = LoginChallenge.objects.select_for_update().filter(id=challenge_id).first()
        if not challenge or challenge.used_at or challenge.expires_at <= timezone.now() or challenge.attempts >= 5:
            return JsonResponse({"error": "authentication_failed"}, status=401)
        challenge.attempts += 1
        challenge.save(update_fields=["attempts", "updated_at"])
        data = _json(request)
        code = str(data.get("code") or "").replace(" ", "")
        recovery = bool(data.get("recovery"))
        scope = "recovery_user" if recovery else "totp_ip"
        subject = str(challenge.user_id) if recovery else f"{challenge.user_id}|{client_ip(request)}"
        try:
            ensure_not_throttled(scope, subject)
        except Throttled:
            AuditEvent.objects.create(severity="WARN", event_type="auth.2fa_throttled", actor=str(challenge.user_id))
            return JsonResponse({"error": "authentication_failed"}, status=401)
        profile = OwnerSecurityProfile.objects.get(user=challenge.user)
        valid = False
        if recovery:
            for rc in RecoveryCode.objects.select_for_update().filter(user=challenge.user, used_at__isnull=True):
                if check_password(code, rc.code_hash):
                    rc.used_at = timezone.now(); rc.save(update_fields=["used_at", "updated_at"]); valid = True; break
        else:
            valid = pyotp.TOTP(decrypt_secret(profile.totp_secret_encrypted)).verify(code, valid_window=1)
        if not valid:
            record_failure(scope, subject)
            AuditEvent.objects.create(severity="WARN", event_type="auth.2fa_failed", actor=str(challenge.user_id))
            return JsonResponse({"error": "authentication_failed"}, status=401)
        reset(scope, subject)
        reset("password_user", challenge.user.get_username())
        challenge.used_at = timezone.now(); challenge.save(update_fields=["used_at", "updated_at"])
        access = issue_access(challenge.user)
        refresh, _ = issue_refresh(challenge.user)
        AuditEvent.objects.create(event_type="auth.login_success", actor=str(challenge.user_id))
    response = JsonResponse({"ok": True})
    _set_auth_cookies(response, access, refresh)
    response.delete_cookie(settings.PREAUTH_COOKIE_NAME, path="/", samesite="Strict")
    return response

@require_POST
def refresh_tokens(request):
    token = request.COOKIES.get(settings.REFRESH_COOKIE_NAME)
    if not token:
        return JsonResponse({"error": "refresh_required"}, status=401)
    try:
        access, refresh, _ = rotate_refresh(token)
    except jwt.PyJWTError:
        response = JsonResponse({"error": "invalid_refresh"}, status=401)
        _clear_auth_cookies(response)
        return response
    response = JsonResponse({"ok": True})
    _set_auth_cookies(response, access, refresh)
    return response

@require_POST
def logout_view(request):
    token = request.COOKIES.get(settings.REFRESH_COOKIE_NAME)
    if token:
        revoke_refresh(token)
    response = JsonResponse({"ok": True})
    _clear_auth_cookies(response)
    return response


@require_POST
def reauthenticate(request):
    owner = getattr(request, "owner", None)
    if not owner:
        return JsonResponse({"error": "unauthorized"}, status=401)
    subject = str(owner.pk)
    try:
        ensure_not_throttled("reauth_user", subject)
    except Throttled:
        return JsonResponse({"error": "reauthentication_failed"}, status=401)
    data = _json(request)
    if not verify_reauthentication(owner, str(data.get("password") or ""), str(data.get("code") or "")):
        record_failure("reauth_user", subject)
        AuditEvent.objects.create(severity="WARN", event_type="auth.reauthentication_failed", actor=subject)
        return JsonResponse({"error": "reauthentication_failed"}, status=401)
    reset("reauth_user", subject)
    token = issue_reauthentication_grant(owner, ["security_reset"])
    response = JsonResponse({"ok": True, "expires_in": int(__import__("os").getenv("REAUTH_GRANT_SECONDS", "300"))})
    response.set_cookie(
        "amarktai_reauth", token, max_age=int(__import__("os").getenv("REAUTH_GRANT_SECONDS", "300")),
        secure=settings.SESSION_COOKIE_SECURE, httponly=True, samesite="Strict", path="/",
    )
    AuditEvent.objects.create(event_type="auth.reauthentication_success", actor=subject)
    return response


@require_POST
def security_reset(request):
    owner = getattr(request, "owner", None)
    if not owner:
        return JsonResponse({"error": "unauthorized"}, status=401)
    token = request.COOKIES.get("amarktai_reauth", "")
    if not consume_reauthentication_grant(owner, token, "security_reset"):
        return JsonResponse({"error": "recent_reauthentication_required"}, status=403)
    now = timezone.now()
    with transaction.atomic():
        RefreshSession.objects.filter(user=owner, revoked_at__isnull=True).update(revoked_at=now)
        ReauthenticationGrant.objects.filter(user=owner, revoked_at__isnull=True).update(revoked_at=now)
        profile = OwnerSecurityProfile.objects.select_for_update().get(user=owner)
        profile.security_version += 1
        profile.save(update_fields=["security_version", "updated_at"])
        AuditEvent.objects.create(severity="WARN", event_type="auth.security_reset", actor=str(owner.pk))
    response = JsonResponse({"ok": True})
    _clear_auth_cookies(response)
    return response

@require_GET
def overview_api(request):
    if not getattr(request, "owner", None):
        return JsonResponse({"error": "unauthorized"}, status=401)
    today = timezone.localdate()
    earned = Payout.objects.filter(
        state__in=[Payout.State.EARNED, Payout.State.PAYOUT_PENDING, Payout.State.SETTLED],
        earned_at__date=today,
    ).aggregate(v=Sum("net"))["v"] or 0
    settled = Payout.objects.filter(state=Payout.State.SETTLED, settled_at__date=today).aggregate(v=Sum("net"))["v"] or 0
    pending = Payout.objects.filter(state=Payout.State.PAYOUT_PENDING).aggregate(v=Sum("net"))["v"] or 0
    genx_used = GenXCall.objects.filter(created_at__date=today).aggregate(v=Sum("credits"))["v"] or 0
    latest_genx = GenXAccountSnapshot.objects.order_by("-created_at").first()
    active_agents = Worker.objects.exclude(status__in=["OFFLINE", "READY"]).count()
    return JsonResponse({
        "autonomous_mode": current_mode().value,
        "net_earned_today": str(earned),
        "settled_today": str(settled),
        "pending_payout": str(pending),
        "active_paid_jobs": Job.objects.filter(state__in=[Job.State.CLAIMED, Job.State.AWARDED, Job.State.EXECUTING]).count(),
        "active_agents": active_agents,
        "genx_balance": None if latest_genx is None or latest_genx.available_credits is None else str(latest_genx.available_credits),
        "genx_used_today": str(genx_used),
        "revenue_truth": "Accepted earnings and pending payout are not received cash. Only SETTLED is received cash; expected opportunity values are never counted here.",
    })
