import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [x.strip() for x in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if x.strip()]
CSRF_TRUSTED_ORIGINS = [x.strip() for x in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if x.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "control",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "control.middleware.JwtOwnerMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

if os.getenv("DJANGO_DB_ENGINE") == "sqlite":
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}
else:
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "amarktai_earn"),
        "USER": os.getenv("POSTGRES_USER", "amarktai"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
        "HOST": os.getenv("POSTGRES_HOST", "postgres"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 14}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
LANGUAGE_CODE = "en-za"
TIME_ZONE = "Africa/Johannesburg"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SECURE_SSL_REDIRECT = os.getenv("AMARKTAI_ENV", "development") == "production"
SESSION_COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") == "1"
CSRF_COOKIE_SAMESITE = "Strict"
SECURE_HSTS_SECONDS = 31536000 if os.getenv("AMARKTAI_ENV") == "production" else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

JWT_ISSUER = os.getenv("JWT_ISSUER", "amarktai-earn")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "amarktai-earn-owner")
JWT_ACTIVE_KID = os.getenv("JWT_ACTIVE_KID", "v1")
JWT_SIGNING_KEYS = json.loads(os.getenv("JWT_SIGNING_KEYS_JSON", '{"v1":"dev-only-change-me"}'))
JWT_ACCESS_SECONDS = int(os.getenv("JWT_ACCESS_SECONDS", "900"))
JWT_REFRESH_SECONDS = int(os.getenv("JWT_REFRESH_SECONDS", "1209600"))
FIELD_ENCRYPTION_ACTIVE_KID = os.getenv("FIELD_ENCRYPTION_ACTIVE_KID", "v1")
FIELD_ENCRYPTION_KEYS = json.loads(os.getenv("FIELD_ENCRYPTION_KEYS_JSON", '{"v1":""}'))
ACCESS_COOKIE_NAME = "amarktai_access"
REFRESH_COOKIE_NAME = "amarktai_refresh"
PREAUTH_COOKIE_NAME = "amarktai_preauth"
