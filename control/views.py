import json
from datetime import timedelta
import pyotp
import jwt
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
from .jwt_auth import issue_access, issue_refresh, rotate_refresh, revoke_refresh
from .models import AuditEvent, Job, LoginChallenge, OwnerSecurityProfile, Payout, RecoveryCode
from .secrets import decrypt_secret

User = get_user_model()


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
    if not getattr(request, "owner", None):
        return redirect("login")
    return render(request, "control/overview.html")

@require_POST
def password_login(request):
    data = _json(request)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    user = authenticate(request, username=username, password=password)
    if not user or not user.is_active or not user.is_staff:
        AuditEvent.objects.create(severity="WARN", event_type="auth.password_failed", actor=username[:120])
        return JsonResponse({"error": "invalid_credentials"}, status=401)
    profile, _ = OwnerSecurityProfile.objects.get_or_create(user=user)
    if not profile.totp_secret_encrypted or not profile.totp_confirmed_at:
        return JsonResponse({"error": "totp_not_enrolled", "message": "Owner TOTP must be enrolled from the server bootstrap flow."}, status=403)
    challenge = LoginChallenge.objects.create(user=user, expires_at=timezone.now() + timedelta(minutes=5))
    response = JsonResponse({"requires_2fa": True})
    response.set_cookie(settings.PREAUTH_COOKIE_NAME, str(challenge.id), max_age=300, secure=settings.SESSION_COOKIE_SECURE, httponly=True, samesite="Strict", path="/")
    return response

@require_POST
def verify_totp(request):
    challenge_id = request.COOKIES.get(settings.PREAUTH_COOKIE_NAME)
    if not challenge_id:
        return JsonResponse({"error": "preauth_required"}, status=401)
    with transaction.atomic():
        challenge = LoginChallenge.objects.select_for_update().filter(id=challenge_id).first()
        if not challenge or challenge.used_at or challenge.expires_at <= timezone.now() or challenge.attempts >= 5:
            return JsonResponse({"error": "invalid_challenge"}, status=401)
        challenge.attempts += 1
        challenge.save(update_fields=["attempts", "updated_at"])
        data = _json(request)
        code = str(data.get("code") or "").replace(" ", "")
        recovery = bool(data.get("recovery"))
        profile = OwnerSecurityProfile.objects.get(user=challenge.user)
        valid = False
        if recovery:
            for rc in RecoveryCode.objects.select_for_update().filter(user=challenge.user, used_at__isnull=True):
                if check_password(code, rc.code_hash):
                    rc.used_at = timezone.now(); rc.save(update_fields=["used_at", "updated_at"]); valid = True; break
        else:
            valid = pyotp.TOTP(decrypt_secret(profile.totp_secret_encrypted)).verify(code, valid_window=1)
        if not valid:
            AuditEvent.objects.create(severity="WARN", event_type="auth.2fa_failed", actor=str(challenge.user_id))
            return JsonResponse({"error": "invalid_2fa"}, status=401)
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
        with transaction.atomic():
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

@require_GET
def overview_api(request):
    if not getattr(request, "owner", None):
        return JsonResponse({"error": "unauthorized"}, status=401)
    today = timezone.localdate()
    earned = Payout.objects.filter(state=Payout.State.EARNED, updated_at__date=today).aggregate(v=Sum("net"))["v"] or 0
    settled = Payout.objects.filter(state=Payout.State.SETTLED, settled_at__date=today).aggregate(v=Sum("net"))["v"] or 0
    pending = Payout.objects.filter(state=Payout.State.PAYOUT_PENDING).aggregate(v=Sum("net"))["v"] or 0
    return JsonResponse({
        "autonomous_mode": __import__("os").getenv("AUTONOMOUS_MODE", "OFF"),
        "net_earned_today": str(earned),
        "settled_today": str(settled),
        "pending_payout": str(pending),
        "active_paid_jobs": Job.objects.filter(state__in=[Job.State.CLAIMED, Job.State.AWARDED, Job.State.EXECUTING]).count(),
        "revenue_truth": "Only SETTLED is received cash; expected values are not counted as settled.",
    })
