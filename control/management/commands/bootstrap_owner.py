import getpass
import secrets
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
import pyotp
from control.models import OwnerSecurityProfile, RecoveryCode, RefreshSession
from control.secrets import encrypt_secret


class Command(BaseCommand):
    help = "Create or securely rotate the single owner account, TOTP enrollment and recovery codes."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="owner")
        parser.add_argument("--email", default="")
        parser.add_argument("--rotate-2fa", action="store_true")

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"].strip()
        email = options["email"].strip()
        existing = User.objects.filter(username=username).first()
        profile = OwnerSecurityProfile.objects.filter(user=existing).first() if existing else None
        if profile and profile.totp_confirmed_at and not options["rotate_2fa"]:
            raise CommandError("owner already has confirmed TOTP; use --rotate-2fa deliberately")

        password = ""
        if existing is None:
            password = getpass.getpass("New owner password (14+ chars): ")
            confirmation = getpass.getpass("Confirm owner password: ")
            if password != confirmation:
                raise CommandError("passwords do not match")
            if len(password) < 14:
                raise CommandError("password must contain at least 14 characters")

        secret = pyotp.random_base32()
        totp = pyotp.TOTP(secret)
        uri = totp.provisioning_uri(name=email or username, issuer_name="Amarktai Earn")
        self.stdout.write("Add this TOTP provisioning URI to your authenticator. It is displayed only during this enrollment:")
        self.stdout.write(uri)
        code = input("Enter the current 6-digit authenticator code: ").strip()
        if not totp.verify(code, valid_window=1):
            raise CommandError("TOTP confirmation failed; no security changes were saved")

        recovery_codes = [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(10)]
        with transaction.atomic():
            if existing is None:
                user = User(username=username, email=email, is_staff=True, is_superuser=True, is_active=True)
                user.set_password(password)
                user.save()
            else:
                user = existing
                changed = []
                if email and user.email != email:
                    user.email = email
                    changed.append("email")
                for field in ("is_staff", "is_superuser", "is_active"):
                    if not getattr(user, field):
                        setattr(user, field, True)
                        changed.append(field)
                if changed:
                    user.save(update_fields=changed)
            profile, profile_created = OwnerSecurityProfile.objects.select_for_update().get_or_create(user=user)
            profile.totp_secret_encrypted = encrypt_secret(secret)
            profile.totp_confirmed_at = timezone.now()
            profile.security_version = 1 if profile_created else profile.security_version + 1
            profile.save()
            RecoveryCode.objects.filter(user=user).delete()
            RecoveryCode.objects.bulk_create([RecoveryCode(user=user, code_hash=make_password(code)) for code in recovery_codes])
            RefreshSession.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())

        self.stdout.write(self.style.SUCCESS(f"Owner {username!r} is configured with mandatory TOTP."))
        self.stdout.write("RECOVERY CODES — save securely now; they will not be shown again:")
        for recovery_code in recovery_codes:
            self.stdout.write(recovery_code)
