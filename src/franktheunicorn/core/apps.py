"""Django app configuration for the core module."""

from django.apps import AppConfig


class CoreConfig(AppConfig):
    """AppConfig for franktheunicorn.core."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "franktheunicorn.core"
    verbose_name = "Frank the Unicorn Core"

    def ready(self) -> None:
        """Log the project allow-list at startup.

        The web process only reads project YAML from inside a request, so
        without this the first sign of a misresolved projects directory is an
        empty dashboard. The worker logs its own copy on the way up.

        Belt and braces around ``load_project_configs``, which is exception-total
        per file: anything escaping it here would abort ``apps.populate``, so a
        config typo would take down ``manage.py check``, ``migrate``, the worker
        and the test run. A log line is not worth that.
        """
        import logging

        from django.conf import settings

        from franktheunicorn.config.loader import load_project_configs

        try:
            load_project_configs(getattr(settings, "FRANK_PROJECTS_DIR", ""))
        except Exception:
            logging.getLogger(__name__).warning(
                "Could not read project configs at startup; carrying on.", exc_info=True
            )
