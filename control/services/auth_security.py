from __future__ import annotations

import hashlib
import os
import secrets
from datetime import timedelta

from django.contrib.auth import authenticate
from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.utils import timezone

from control.models import AuthThrottle, OwnerSecurityProfile, ReauthenticationGrant
from control.secrets import decrypt_secret

import pyotp


class Throttled(RuntimeError):
    pass


def client_ip(request) -> str:
    # REMOTE_ADDR is authoritative unless an explicitly configured proxy has
    # already rewritten it. Never trust arbitrary X-Forwarded-For input here.
    return str(request.META.get("REMOTE_ADDR") or "unknown")[:128]


def _key(scope: str, subject: str) -> str:
    pepper = os.getenv("AUTH_THROTTLE_PEPPER", "amarktai-auth-throttle")
    return hashlib.sha256(f"{pepper}|{scope}|{subject.casefold()}".encode()).hexdigest()


def _policy(scope: str) -> tuple[int, int, int]:
    policies = {
        "password_ip": (5, 300, 900),
        "password_user": (8, 900, 1800),
        "totp_ip": (5, 300, 900),
        "recovery_user": (3, 1800, 3600),
        "reauth_user": (5, 900, 1800),
    }
    attempts, window, cooldown = policies[scope]
    prefix = f"AUTH_{scope.upper()}"
    return (
        int(os.getenv(f"{prefix}_MAX_ATTEMPTS", attempts)),
        int(os.getenv(f"{prefix}_WINDOW_SECONDS", window)),
        int(os.getenv(f"{prefix}_COOLDOWN_SECONDS", cooldown)),
    )


def ensure_not_throttled(scope: str, subject: str) -> None:
    row = AuthThrottle.objects.filter(key_hash=_key(scope, subject)).first()
    if row and row.locked_until and row.locked_until > timezone.now():
        raise Throttled("authentication temporarily unavailable")


@transaction.atomic
def record_failure(scope: str, subject: str) -> None:
    now = timezone.now()
    maximum, window_seconds, cooldown = _policy(scope)
    row, _ = AuthThrottle.objects.select_for_update().get_or_create(
        key_hash=_key(scope, subject), defaults={"scope": scope, "window_started_at": now}
    )
    if row.window_started_at <= now - timedelta(seconds=window_seconds):
        row.failure_count = 0
        row.window_started_at = now
        row.locked_until = None
    row.failure_count += 1
    row.last_failure_at = now
    if row.failure_count >= maximum:
        row.locked_until = now + timedelta(seconds=cooldown)
    row.save()


def reset(scope: str, subject: str) -> None:
    AuthThrottle.objects.filter(key_hash=_key(scope, subject)).delete()


def verify_reauthentication(user, password: str, totp_code: str) -> bool:
    authenticated = authenticate(username=user.get_username(), password=password)
    if authenticated is None or authenticated.pk != user.pk:
        return False
    try:
        profile = OwnerSecurityProfile.objects.get(user=user)
        secret = decrypt_secret(profile.totp_secret_encrypted)
    except Exception:
        return False
    return bool(profile.totp_confirmed_at and pyotp.TOTP(secret).verify(totp_code.replace(" ", ""), valid_window=1))


def issue_reauthentication_grant(user, actions: list[str]) -> str:
    token = secrets.token_urlsafe(32)
    ReauthenticationGrant.objects.create(
        user=user,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        allowed_actions=sorted(set(actions)),
        expires_at=timezone.now() + timedelta(seconds=int(os.getenv("REAUTH_GRANT_SECONDS", "300"))),
    )
    return token


@transaction.atomic
def consume_reauthentication_grant(user, token: str, action: str) -> bool:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    grant = ReauthenticationGrant.objects.select_for_update().filter(token_hash=token_hash, user=user).first()
    if not grant or grant.used_at or grant.revoked_at or grant.expires_at <= timezone.now() or action not in grant.allowed_actions:
        return False
    grant.used_at = timezone.now()
    grant.save(update_fields=["used_at", "updated_at"])
    return True
