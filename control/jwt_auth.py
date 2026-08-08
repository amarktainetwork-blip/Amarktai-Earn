import hashlib
import uuid
from datetime import timedelta
import jwt
from django.db import transaction
from django.conf import settings
from django.utils import timezone
from .models import RefreshSession


def _key_for(kid: str) -> str:
    try:
        return settings.JWT_SIGNING_KEYS[kid]
    except KeyError as exc:
        raise jwt.InvalidKeyError("unknown signing key") from exc


def issue_access(user) -> str:
    now = timezone.now()
    payload = {
        "sub": str(user.pk), "type": "access", "jti": str(uuid.uuid4()),
        "iss": settings.JWT_ISSUER, "aud": settings.JWT_AUDIENCE,
        "iat": int(now.timestamp()), "exp": int((now + timedelta(seconds=settings.JWT_ACCESS_SECONDS)).timestamp()),
    }
    kid = settings.JWT_ACTIVE_KID
    return jwt.encode(payload, _key_for(kid), algorithm="HS256", headers={"kid": kid})


def issue_refresh(user, family_id=None) -> tuple[str, RefreshSession]:
    now = timezone.now()
    family_id = family_id or uuid.uuid4()
    session = RefreshSession.objects.create(user=user, family_id=family_id, expires_at=now + timedelta(seconds=settings.JWT_REFRESH_SECONDS))
    payload = {
        "sub": str(user.pk), "type": "refresh", "jti": str(session.jti), "family": str(family_id),
        "iss": settings.JWT_ISSUER, "aud": settings.JWT_AUDIENCE,
        "iat": int(now.timestamp()), "exp": int(session.expires_at.timestamp()),
    }
    kid = settings.JWT_ACTIVE_KID
    return jwt.encode(payload, _key_for(kid), algorithm="HS256", headers={"kid": kid}), session


def decode_token(token: str, expected_type: str) -> dict:
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    if not kid:
        raise jwt.InvalidTokenError("missing kid")
    payload = jwt.decode(token, _key_for(kid), algorithms=["HS256"], audience=settings.JWT_AUDIENCE, issuer=settings.JWT_ISSUER)
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def rotate_refresh(token: str):
    payload = decode_token(token, "refresh")
    jti = uuid.UUID(payload["jti"])
    family = uuid.UUID(payload["family"])
    reuse_detected = False
    expired = False
    result = None
    # Own the transaction here. In particular, family revocation on replay must
    # commit before the caller receives an InvalidTokenError; otherwise an outer
    # atomic block would roll back the security response that the error triggered.
    with transaction.atomic():
        try:
            session = RefreshSession.objects.select_for_update().get(jti=jti)
        except RefreshSession.DoesNotExist as exc:
            raise jwt.InvalidTokenError("unknown refresh session") from exc
        if session.revoked_at or session.replaced_by_jti:
            RefreshSession.objects.filter(family_id=family, revoked_at__isnull=True).update(revoked_at=timezone.now())
            reuse_detected = True
        elif session.expires_at <= timezone.now():
            expired = True
        else:
            new_token, new_session = issue_refresh(session.user, family_id=family)
            session.revoked_at = timezone.now()
            session.replaced_by_jti = new_session.jti
            session.save(update_fields=["revoked_at", "replaced_by_jti", "updated_at"])
            result = (issue_access(session.user), new_token, new_session)
    if reuse_detected:
        raise jwt.InvalidTokenError("refresh token reuse detected")
    if expired:
        raise jwt.ExpiredSignatureError("refresh expired")
    return result


def revoke_refresh(token: str) -> None:
    try:
        payload = decode_token(token, "refresh")
        RefreshSession.objects.filter(jti=payload["jti"], revoked_at__isnull=True).update(revoked_at=timezone.now())
    except Exception:
        pass


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()
