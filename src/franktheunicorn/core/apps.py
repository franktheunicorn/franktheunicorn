"""Django app configuration for the core module."""

from django.apps import AppConfig


class CoreConfig(AppConfig):
    """AppConfig for franktheunicorn.core."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "franktheunicorn.core"
    verbose_name = "Frank the Unicorn Core"
