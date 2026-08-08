from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

class SecretEncryptionError(RuntimeError):
    pass

def encrypt_secret(value: str) -> str:
    kid = settings.FIELD_ENCRYPTION_ACTIVE_KID
    key = settings.FIELD_ENCRYPTION_KEYS.get(kid)
    if not key:
        raise SecretEncryptionError("field encryption key is not configured")
    token = Fernet(key.encode()).encrypt(value.encode()).decode()
    return f"{kid}:{token}"

def decrypt_secret(value: str) -> str:
    try:
        kid, token = value.split(":", 1)
        key = settings.FIELD_ENCRYPTION_KEYS[kid]
        return Fernet(key.encode()).decrypt(token.encode()).decode()
    except (ValueError, KeyError, InvalidToken) as exc:
        raise SecretEncryptionError("unable to decrypt secret") from exc
