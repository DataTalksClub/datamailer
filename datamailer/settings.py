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


def float_env(name, *, default):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return float(value)


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
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
AUTH_BASE_URL = os.environ.get("AUTH_BASE_URL", "" if DEBUG else "https://auth.dtcdev.click").rstrip("/")
AUTH_CLIENT_ID = os.environ.get("AUTH_CLIENT_ID", "" if DEBUG else "41gcnc18qjtq61rsag9iqoepmk")
AUTH_CALLBACK_URL = os.environ.get(
    "AUTH_CALLBACK_URL", "" if DEBUG else "https://datamailer.dtcdev.click/auth/callback"
)
AUTH_LOGOUT_URL = os.environ.get("AUTH_LOGOUT_URL", "" if DEBUG else "https://datamailer.dtcdev.click/")
AUTH_ISSUER = os.environ.get(
    "AUTH_ISSUER", "" if DEBUG else "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_H7nJu52Bs"
).rstrip("/")
AUTH_JWKS_URL = os.environ.get(
    "AUTH_JWKS_URL", f"{AUTH_ISSUER}/.well-known/jwks.json" if AUTH_ISSUER else ""
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_tasks",
    "django_tasks_db",
    "taskdeck",
    "mailing",
]

# Background tasks. Application code depends only on `django.tasks`; the
# backend below is a configuration choice, not an import anywhere in `mailing`.
TASKS = {
    "default": {
        "BACKEND": os.environ.get(
            "TASKS_BACKEND", "django_tasks_db.backend.DatabaseBackend"
        ),
    }
}

# Identifies this project in the taskdeck status contract and in TaskRun rows.
TASKDECK_PROJECT = "datamailer"

# Bearer token the cross-project console presents to read the status
# contract. Empty disables the endpoint entirely (it 404s), so a host that
# has not been given a token cannot leak operational detail.
TASKDECK_STATUS_TOKEN = os.environ.get("TASKDECK_STATUS_TOKEN", "")

# Recurring sends that something outside this service triggers on a timer.
#
# Datamailer has no scheduler and should not grow one: the caller decides who
# gets a reminder and when, and it already owns a schedule. What was missing is
# that a schedule which silently stops firing looked exactly like a quiet
# period. Declaring the expectation here lets the status contract report the
# last run against it, so a missed sweep is visible.
#
# `template_key` is how a run is recognised: the batch task records it in the
# run's message. `cron` and `max_age_s` are what the trigger is *expected* to
# do -- keep them in step with the EventBridge rules in
# aws-infra main/cmp/cmp_deadline_reminder.tf, which are the source of truth.
TASKDECK_SCHEDULES = [
    {
        "name": "deadline-reminders",
        "template_key": "deadline-reminder",
        "cron": "0 9 * * *",
        "max_age_s": 60 * 60 * 26,  # daily, with room for a late or slow run
    },
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
                "mailing.context_processors.operator_client_context",
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

if DATABASES["default"]["ENGINE"].endswith("sqlite3"):
    # On a single host this database has many writers: two gunicorn workers,
    # the task worker, and five queue workers. SQLite's default is to fail
    # immediately when another connection holds the write lock, which surfaces
    # as "database is locked" on a bulk send rather than as a wait.
    #
    # WAL lets readers continue while one writer works, `timeout` makes a
    # blocked writer wait instead of raising, and IMMEDIATE takes the write
    # lock when a write transaction opens rather than upgrading mid-way --
    # the upgrade is what turns two concurrent transactions into an error
    # neither can retry.
    #
    # None of this makes SQLite a good fit for this many writers. It buys room
    # until the move to Postgres; see docs/managed-database-migration.md.
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"] |= {
        "timeout": 30,
        "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
        "transaction_mode": "IMMEDIATE",
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
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
if TESTING:
    STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "newsletter@example.com")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
API_DOCS_BASE_URL = os.environ.get("DATAMAILER_API_DOCS_BASE_URL", PUBLIC_BASE_URL).rstrip("/")
AWS_SES_CONFIGURATION_SET = os.environ.get("AWS_SES_CONFIGURATION_SET", "")
SES_MAX_SEND_RATE_PER_SECOND = float_env("DATAMAILER_SES_MAX_SEND_RATE", default=10.0)
SQS_TRANSACTIONAL_EMAIL_QUEUE_URL = os.environ.get("SQS_TRANSACTIONAL_EMAIL_QUEUE_URL", "")
SQS_CAMPAIGN_EMAIL_QUEUE_URL = os.environ.get("SQS_CAMPAIGN_EMAIL_QUEUE_URL", "")
SQS_EMAIL_EVENTS_QUEUE_URL = os.environ.get("SQS_EMAIL_EVENTS_QUEUE_URL", "")
SQS_SES_WEBHOOKS_QUEUE_URL = os.environ.get("SQS_SES_WEBHOOKS_QUEUE_URL", "")
SQS_INBOUND_EMAIL_QUEUE_URL = os.environ.get("SQS_INBOUND_EMAIL_QUEUE_URL", "")
INBOUND_EMAIL_EVENTS_TOPIC_ARN = os.environ.get("INBOUND_EMAIL_EVENTS_TOPIC_ARN", "")
INBOUND_EMAIL_IDEMPOTENCY_TABLE = os.environ.get("INBOUND_EMAIL_IDEMPOTENCY_TABLE", "")
INBOUND_EMAIL_ARTIFACT_PREFIX = os.environ.get("INBOUND_EMAIL_ARTIFACT_PREFIX", "processed/")
INBOUND_EMAIL_INLINE_BODY_MAX_BYTES = int(os.environ.get("INBOUND_EMAIL_INLINE_BODY_MAX_BYTES", "65536"))
INBOUND_EMAIL_ROUTES = {
    address.strip().lower(): route.strip()
    for address, separator, route in (
        item.partition("=") for item in os.environ.get("INBOUND_EMAIL_ROUTES", "").split(",") if item.strip()
    )
    if separator and address.strip() and route.strip()
}
INBOUND_EMAIL_ROUTE_DOMAINS = {
    domain.strip().lower()
    for domain in os.environ.get("INBOUND_EMAIL_ROUTE_DOMAINS", "").split(",") if domain.strip()
}
WORKER_STATUS_SYSTEMD_ENABLED = bool_env("DATAMAILER_WORKER_STATUS_SYSTEMD_ENABLED", default=not TESTING)
WORKER_STATUS_SYSTEMD_TIMEOUT_SECONDS = float_env("DATAMAILER_WORKER_STATUS_SYSTEMD_TIMEOUT_SECONDS", default=1.5)
TRANSACTIONAL_EMAIL_QUEUE_NAME = os.environ.get("TRANSACTIONAL_EMAIL_QUEUE_NAME", "transactional-email")
CAMPAIGN_EMAIL_QUEUE_NAME = os.environ.get("CAMPAIGN_EMAIL_QUEUE_NAME", "campaign-email")
SES_WEBHOOKS_QUEUE_NAME = os.environ.get("SES_WEBHOOKS_QUEUE_NAME", "ses-webhooks")
EMAIL_EVENTS_QUEUE_NAME = os.environ.get("EMAIL_EVENTS_QUEUE_NAME", "email-events")
CMP_WEBHOOK_URL = os.environ.get("CMP_WEBHOOK_URL", "").strip()
CMP_WEBHOOK_TOKEN = os.environ.get("CMP_WEBHOOK_TOKEN", "")
CMP_WEBHOOK_TIMEOUT_SECONDS = float_env("CMP_WEBHOOK_TIMEOUT_SECONDS", default=3.0)
MAILCHIMP_TIMEOUT_SECONDS = float_env("MAILCHIMP_TIMEOUT_SECONDS", default=5.0)
SES_WEBHOOKS_SIGNATURE_MODE = os.environ.get(
    "SES_WEBHOOKS_SIGNATURE_MODE",
    "mock" if DEBUG or TESTING else "strict",
)
SES_WEBHOOKS_ALLOW_SUBSCRIPTION_CONFIRMATION = bool_env("SES_WEBHOOKS_ALLOW_SUBSCRIPTION_CONFIRMATION", default=False)
