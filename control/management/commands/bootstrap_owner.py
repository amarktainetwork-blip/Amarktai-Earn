import secrets
import pyotp
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from control.models import OwnerSecurityProfile, RecoveryCode
from control.secrets import encrypt_secret

class Command(BaseCommand):
    help = "Create/update the single owner and enroll mandatory TOTP."
    def add_arguments(self, parser):
        parser.add_argument("--username")
        parser.add_argument("--email")
        parser.add_argument("--password")
    def handle(self, *args, **opts):
        username = opts.get("username") or input("Owner username: ").strip()
        email = opts.get("email") or input("Owner email: ").strip()
        password = opts.get("password")
        if not password:
            import getpass
            password = getpass.getpass("Owner password (14+ chars): ")
        if len(password) < 14:
            raise CommandError("Password must be at least 14 characters")
        User = get_user_model()
        user, _ = User.objects.get_or_create(username=username, defaults={"email": email, "is_staff": True, "is_superuser": True})
        user.email = email; user.is_staff = True; user.is_superuser = True; user.set_password(password); user.save()
        secret = pyotp.random_base32()
        profile, _ = OwnerSecurityProfile.objects.get_or_create(user=user)
        profile.totp_secret_encrypted = encrypt_secret(secret)
        profile.totp_confirmed_at = timezone.now(); profile.save()
        RecoveryCode.objects.filter(user=user, used_at__isnull=True).delete()
        codes = [secrets.token_hex(5) for _ in range(10)]
        RecoveryCode.objects.bulk_create([RecoveryCode(user=user, code_hash=make_password(c)) for c in codes])
        self.stdout.write(self.style.WARNING("Enroll this TOTP URI once, then store it securely:"))
        self.stdout.write(pyotp.TOTP(secret).provisioning_uri(name=email or username, issuer_name="Amarktai Earn"))
        self.stdout.write(self.style.WARNING("Recovery codes (shown once):"))
        self.stdout.write("\n".join(codes))
