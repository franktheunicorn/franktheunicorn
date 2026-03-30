"""WSGI config for franktheunicorn."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "franktheunicorn.settings")

application = get_wsgi_application()
