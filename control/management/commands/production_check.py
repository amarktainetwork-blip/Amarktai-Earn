import os

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


DOMAIN = "earn.amarktai.co.za"
VALID_AUTONOMY_MODES = {"OFF", "SHADOW", "MANUAL", "LOW_RISK", "FULL"}


def _placeholder(value: str) -> bool:
    lowered = value.casefold()
    return not value or "replace" in lowered or "change-me" in lowered or "dev-only" in lowered


class Command(BaseCommand):
    help = "Fail closed when production security/configuration invariants are not satisfied."

    def handle(self, *args, **options):
        environment = os.getenv("AMARKTAI_ENV", "development").lower()
        if environment != "production":
            self.stdout.write(f"production preflight skipped for AMARKTAI_ENV={environment}")
            return

        errors: list[str] = []
        secret_key = str(settings.SECRET_KEY)
        if _placeholder(secret_key) or len(secret_key) < 50:
            errors.append("DJANGO_SECRET_KEY must be a non-placeholder secret of at least 50 characters")
        if settings.DEBUG:
            errors.append("DJANGO_DEBUG must be disabled")
        if not settings.SESSION_COOKIE_SECURE or not settings.CSRF_COOKIE_SECURE:
            errors.append("secure cookies must be enabled")
        if DOMAIN not in settings.ALLOWED_HOSTS:
            errors.append(f"DJANGO_ALLOWED_HOSTS must include {DOMAIN}")
        if f"https://{DOMAIN}" not in settings.CSRF_TRUSTED_ORIGINS:
            errors.append(f"DJANGO_CSRF_TRUSTED_ORIGINS must include https://{DOMAIN}")

        jwt_key = str(settings.JWT_SIGNING_KEYS.get(settings.JWT_ACTIVE_KID, ""))
        if _placeholder(jwt_key) or len(jwt_key.encode()) < 64:
            errors.append("active JWT signing key must contain at least 64 non-placeholder bytes")
        field_key = str(settings.FIELD_ENCRYPTION_KEYS.get(settings.FIELD_ENCRYPTION_ACTIVE_KID, ""))
        try:
            Fernet(field_key.encode())
        except Exception:
            errors.append("active field-encryption key must be a valid Fernet key")

        database_password = os.getenv("POSTGRES_PASSWORD", "")
        if _placeholder(database_password) or len(database_password) < 16:
            errors.append("POSTGRES_PASSWORD must be a non-placeholder secret of at least 16 characters")
        if not os.getenv("POSTGRES_HOST", ""):
            errors.append("POSTGRES_HOST is required")
        backup_passphrase = os.getenv("BACKUP_PASSPHRASE", "")
        if _placeholder(backup_passphrase) or len(backup_passphrase) < 32:
            errors.append("BACKUP_PASSPHRASE must be a non-placeholder secret of at least 32 characters")
        throttle_pepper = os.getenv("AUTH_THROTTLE_PEPPER", "")
        if _placeholder(throttle_pepper) or len(throttle_pepper.encode()) < 32:
            errors.append("AUTH_THROTTLE_PEPPER must contain at least 32 non-placeholder bytes")

        mode = os.getenv("AUTONOMOUS_MODE", "OFF").upper()
        if mode not in VALID_AUTONOMY_MODES:
            errors.append("AUTONOMOUS_MODE must be OFF, SHADOW, MANUAL, LOW_RISK, or FULL")
        if os.getenv("AGENTGIGS_AUTO_APPLY_ENABLED", "0") == "1" and mode not in {"LOW_RISK", "FULL"}:
            errors.append("AgentGigs auto-apply requires AUTONOMOUS_MODE=LOW_RISK or FULL")
        if os.getenv("INBOUND_SERVICE_AUTO_ACCEPT_ENABLED", "0") == "1" and mode not in {"LOW_RISK", "FULL"}:
            errors.append("Inbound service auto-accept requires AUTONOMOUS_MODE=LOW_RISK or FULL")

        if os.getenv("SANDBOX_CODING_ENABLED", "0") == "1":
            sandbox_token = os.getenv("SANDBOX_TOKEN_SECRET", "")
            broker_secret = os.getenv("SANDBOX_BROKER_SECRET", "")
            if _placeholder(sandbox_token) or len(sandbox_token.encode()) < 32:
                errors.append("SANDBOX_TOKEN_SECRET must contain at least 32 non-placeholder bytes when coding sandboxes are enabled")
            if _placeholder(broker_secret) or len(broker_secret.encode()) < 32:
                errors.append("SANDBOX_BROKER_SECRET must contain at least 32 non-placeholder bytes when coding sandboxes are enabled")
            if not os.getenv("GENX_API_KEY", "").strip():
                errors.append("GENX_API_KEY is required when coding sandboxes are enabled")
        if os.getenv("DEPENDENCY_PREPARATION_ENABLED", "0") == "1" and os.getenv("SANDBOX_CODING_ENABLED", "0") != "1":
            errors.append("dependency preparation requires SANDBOX_CODING_ENABLED=1")

        if errors:
            raise CommandError("Production preflight failed: " + "; ".join(errors))
        self.stdout.write(self.style.SUCCESS(f"production preflight passed mode={mode}"))
