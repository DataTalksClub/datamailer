import os
import sys
import warnings
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def csv_env(name, default="", *, allow_empty=False):
    values = [value.strip() for value in os.environ.get(name, default).split(",") if value.strip()]
    if values or allow_empty:
        return values
    return [value.strip() for value in default.split(",") if value.strip()]


def bool_env(name, *, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes"}


DEBUG = bool_env("DEBUG", default=True)
TESTING = "test" in sys.argv or "pytest" in sys.argv[0]

DEV_FALLBACK_SECRET_KEY = "django-insecure-dev-only-do-not-use-in-production"
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
if not SECRET_KEY:
    if TESTING:
        SECRET_KEY = DEV_FALLBACK_SECRET_KEY
    elif DEBUG:
        warnings.warn("SECRET_KEY is not set; using a development-only fallback.", RuntimeWarning, stacklevel=2)
        SECRET_KEY = DEV_FALLBACK_SECRET_KEY
    else:
        raise ImproperlyConfigured("SECRET_KEY must be set when DEBUG=False.")

ALLOWED_HOSTS = csv_env("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
CSRF_TRUSTED_ORIGINS = csv_env("CSRF_TRUSTED_ORIGINS", "", allow_empty=True)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "mailing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if TESTING:
    MIDDLEWARE.remove("whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "datamailer.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "datamailer.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "newsletter@example.com")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
AWS_SES_CONFIGURATION_SET = os.environ.get("AWS_SES_CONFIGURATION_SET", "")
SQS_TRANSACTIONAL_EMAIL_QUEUE_URL = os.environ.get("SQS_TRANSACTIONAL_EMAIL_QUEUE_URL", "")
SQS_CAMPAIGN_EMAIL_QUEUE_URL = os.environ.get("SQS_CAMPAIGN_EMAIL_QUEUE_URL", "")
SQS_EMAIL_EVENTS_QUEUE_URL = os.environ.get("SQS_EMAIL_EVENTS_QUEUE_URL", "")
SQS_SES_WEBHOOKS_QUEUE_URL = os.environ.get("SQS_SES_WEBHOOKS_QUEUE_URL", "")
TRANSACTIONAL_EMAIL_QUEUE_NAME = os.environ.get("TRANSACTIONAL_EMAIL_QUEUE_NAME", "transactional-email")
CAMPAIGN_EMAIL_QUEUE_NAME = os.environ.get("CAMPAIGN_EMAIL_QUEUE_NAME", "campaign-email")
SES_WEBHOOKS_QUEUE_NAME = os.environ.get("SES_WEBHOOKS_QUEUE_NAME", "ses-webhooks")
EMAIL_EVENTS_QUEUE_NAME = os.environ.get("EMAIL_EVENTS_QUEUE_NAME", "email-events")
