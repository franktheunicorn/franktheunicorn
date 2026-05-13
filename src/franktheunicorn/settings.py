"""
Django settings for franktheunicorn.

Local-first: SQLite is the default and only supported database.
All persistent state lives under DATA_DIR, which defaults to ./data/.

App configuration lives in ``operator.yaml`` — this file reads it via
the config resolver.  Only Django-infrastructure settings (secret key,
debug flag, allowed hosts, database URL) are still read from env vars.
"""

import os
from pathlib import Path

import dj_database_url

from franktheunicorn.config.resolver import resolve_config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# --- Django infrastructure (env vars) ---

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "dev-insecure-key-change-me-in-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() in ("true", "1", "yes")

ALLOWED_HOSTS: list[str] = [
    stripped
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")
    if (stripped := h.lstrip())
]

# --- Load unified config from operator.yaml ---

_operator_config, _resolved = resolve_config(BASE_DIR)

# Local state directory — mounted as a volume in Docker.
DATA_DIR = Path(str(_resolved["data_dir"]))
DATA_DIR.mkdir(parents=True, exist_ok=True)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "franktheunicorn.core",
    "franktheunicorn.dashboard",
    "franktheunicorn.digest",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "franktheunicorn.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "franktheunicorn.dashboard.context_processors.nav_projects",
            ],
        },
    },
]

WSGI_APPLICATION = "franktheunicorn.wsgi.application"

# Database: honours DATABASE_URL when set, otherwise SQLite (local-first default).
_DATABASE_URL = os.environ.get("DATABASE_URL")
if _DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(_DATABASE_URL)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(DATA_DIR / "frank.sqlite3"),
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Static files (CSS, JavaScript, Images)
STATIC_URL = "static/"

# --- franktheunicorn-specific settings (from operator.yaml) ---

FRANK_OPERATOR_CONFIG: str = str(_resolved["config_path"])
FRANK_PROJECTS_DIR: str = str(_resolved["projects_dir"])
FRANK_GITHUB_TOKEN: str = str(_resolved["github_token"])
FRANK_MOCK_MODE: bool = bool(_resolved["mock_mode"])
FRANK_FIXTURES_DIR: str = str(_resolved["fixtures_dir"])
FRANK_REPOS_DIR = Path(str(_resolved["repos_dir"]))
FRANK_POLL_INTERVAL: int = int(_resolved["poll_interval"])
FRANK_LOG_LEVEL: str = str(_resolved["log_level"])
FRANK_DIGEST_EMAIL: str = str(_resolved["digest_email"])

# Email settings for digest (optional — skip silently if not configured)
EMAIL_HOST: str = str(_resolved["email_host"])
EMAIL_PORT: int = int(_resolved["email_port"])
EMAIL_HOST_USER: str = str(_resolved["email_host_user"])
EMAIL_HOST_PASSWORD: str = str(_resolved["email_host_password"])
EMAIL_USE_TLS: bool = bool(_resolved["email_use_tls"])
DEFAULT_FROM_EMAIL: str = str(_resolved["email_from"])

USE_TZ = True
TIME_ZONE = "UTC"

# --- Logging ---
#
# Configures the root logger and the franktheunicorn package logger via
# Django's LOGGING dict, so structured log output works in all processes
# (web, worker, management commands, tests).
#
# Level comes from operator.yaml's ``log_level`` (default: INFO).
# Override at runtime with ``FRANK_LOG_LEVEL`` env var or the worker's
# ``--log-level`` / ``--debug`` CLI flags.

_LOG_LEVEL = FRANK_LOG_LEVEL  # already resolved above (str like "INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "frank": {
            "format": "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "frank",
            # No level set — accepts all records that reach this handler.
        },
    },
    "loggers": {
        # franktheunicorn package — honour FRANK_LOG_LEVEL.
        # propagate=True so pytest's caplog fixture can capture log records;
        # the console handler on the root logger emits them.
        "franktheunicorn": {
            "level": _LOG_LEVEL,
            "propagate": True,
        },
        # Django's own loggers — keep INFO in debug mode, WARNING otherwise.
        "django": {
            "level": "INFO" if DEBUG else "WARNING",
            "propagate": True,
        },
    },
    # Root logger: all propagated records arrive here.  Third-party library
    # loggers below WARNING are effectively silenced because they inherit the
    # root level (WARNING) and never create records in the first place.
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
}
