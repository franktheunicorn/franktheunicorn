"""
Django settings for franktheunicorn.

Local-first: SQLite is the default and only supported database.
All persistent state lives under DATA_DIR, which defaults to ./data/.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Local state directory — mounted as a volume in Docker.
DATA_DIR = Path(os.environ.get("FRANK_DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "dev-insecure-key-change-me-in-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() in ("true", "1", "yes")

ALLOWED_HOSTS: list[str] = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "franktheunicorn.core",
    "franktheunicorn.dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "franktheunicorn.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "franktheunicorn.wsgi.application"

# SQLite — the only database. Local-first, no Postgres.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(DATA_DIR / "frank.sqlite3"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Static files (CSS, JavaScript, Images)
STATIC_URL = "static/"

# --- franktheunicorn-specific settings ---

# Path to operator config YAML
FRANK_OPERATOR_CONFIG = os.environ.get(
    "FRANK_OPERATOR_CONFIG",
    str(BASE_DIR / "configs" / "examples" / "operator.yaml"),
)

# Directory containing per-project YAML configs
FRANK_PROJECTS_DIR = os.environ.get(
    "FRANK_PROJECTS_DIR",
    str(BASE_DIR / "configs" / "examples" / "projects"),
)

# GitHub API token (optional — mock mode works without it)
FRANK_GITHUB_TOKEN = os.environ.get("FRANK_GITHUB_TOKEN", "")

# Enable mock/demo mode with fixture data instead of real GitHub API
FRANK_MOCK_MODE = os.environ.get("FRANK_MOCK_MODE", "true").lower() in ("true", "1", "yes")

# Directory containing fixture JSON for mock mode
FRANK_FIXTURES_DIR = os.environ.get(
    "FRANK_FIXTURES_DIR",
    str(BASE_DIR / "configs" / "fixtures"),
)

# Worker polling interval in seconds
FRANK_POLL_INTERVAL = int(os.environ.get("FRANK_POLL_INTERVAL", "300"))

USE_TZ = True
TIME_ZONE = "UTC"
