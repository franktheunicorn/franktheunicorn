"""
Django settings for franktheunicorn.

Local-first: SQLite is the default and only supported database.
All persistent state lives under DATA_DIR, which defaults to ./data/.
"""

import os
from pathlib import Path


def _env_bool(key: str, default: str = "true") -> bool:
    """Parse a boolean from an environment variable."""
    return os.environ.get(key, default).lower() in ("true", "1", "yes")


BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Local state directory — mounted as a volume in Docker.
DATA_DIR = Path(os.environ.get("FRANK_DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "dev-insecure-key-change-me-in-production",
)

DEBUG = _env_bool("DJANGO_DEBUG")

ALLOWED_HOSTS: list[str] = [
    stripped  # pylint: disable=used-before-assignment
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")
    if (stripped := h.lstrip())
]

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
    str(BASE_DIR / "config" / "examples" / "operator.yaml"),
)

# Directory containing per-project YAML configs
FRANK_PROJECTS_DIR = os.environ.get(
    "FRANK_PROJECTS_DIR",
    str(BASE_DIR / "config" / "examples" / "projects"),
)

# GitHub API token (optional — mock mode works without it)
FRANK_GITHUB_TOKEN = os.environ.get("FRANK_GITHUB_TOKEN", "")

# Enable mock/demo mode with fixture data instead of real GitHub API
FRANK_MOCK_MODE = _env_bool("FRANK_MOCK_MODE")

# Directory containing fixture JSON for mock mode
FRANK_FIXTURES_DIR = os.environ.get(
    "FRANK_FIXTURES_DIR",
    str(BASE_DIR / "config" / "fixtures"),
)

# Directory containing local clones of monitored repos (for copy-pasta scanning)
FRANK_REPOS_DIR = Path(os.environ.get("FRANK_REPOS_DIR", str(DATA_DIR / "repos")))

# Worker polling interval in seconds
FRANK_POLL_INTERVAL = int(os.environ.get("FRANK_POLL_INTERVAL", "300"))

# Email settings for digest (optional — skip silently if not configured)
EMAIL_HOST = os.environ.get("REVIEW_AGENT_SMTP_HOST", "")
EMAIL_PORT = int(os.environ.get("REVIEW_AGENT_SMTP_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("REVIEW_AGENT_SMTP_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("REVIEW_AGENT_SMTP_PASS", "")
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = os.environ.get("REVIEW_AGENT_EMAIL_FROM", "frank@localhost")
FRANK_DIGEST_EMAIL = os.environ.get("FRANK_DIGEST_EMAIL", "")

USE_TZ = True
TIME_ZONE = "UTC"
