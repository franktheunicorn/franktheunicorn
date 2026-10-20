"""Run the dashboard dev server on a preferred port, falling back if it's busy.

Thin wrapper around Django's ``runserver``. With no explicit address it serves
on :data:`PREFERRED_PORT` (7742, the same port docker-compose publishes), and if
that port is already in use it falls back to :data:`FALLBACK_PORT` (4242) and
says so, so a second frank — or anything else sitting on 7742 — doesn't make
``make serve`` die with "That port is already in use."

Usage::

    python manage.py serve                 # auto: 7742, else 4242, on 127.0.0.1
    python manage.py serve 0.0.0.0          # auto-pick, bound on 0.0.0.0 (Tailscale)
    python manage.py serve 9000             # explicit port, no fallback
    python manage.py serve 0.0.0.0:9000     # explicit addr:port, no fallback
"""

from __future__ import annotations

import os
import socket

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandParser

PREFERRED_PORT = 7742
FALLBACK_PORT = 4242
DEFAULT_HOST = "127.0.0.1"

#: Set on the resolved port once chosen, so the autoreloader's re-exec reuses it
#: rather than re-picking (and re-printing) on every file change — and never
#: silently hops to 7742 mid-session just because whatever held it went away.
_RESOLVED_PORT_ENV = "FRANK_SERVE_RESOLVED_PORT"


def is_port_free(host: str, port: int) -> bool:
    """Whether *port* can be bound on *host* right now.

    Uses ``SO_REUSEADDR`` so a port left in ``TIME_WAIT`` by a just-exited
    server still reads as free, matching what ``runserver`` itself can bind.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True


def choose_port(
    host: str, preferred: int = PREFERRED_PORT, fallback: int = FALLBACK_PORT
) -> tuple[int, str]:
    """Pick a port on *host*: ``preferred`` if free, else ``fallback``.

    Returns ``(port, status)`` where status is ``"preferred"`` (preferred is
    free), ``"fallback"`` (preferred busy, fallback free), or ``"both-busy"``
    (neither free — fallback is returned anyway so the caller can let
    ``runserver`` surface the real bind error).
    """
    if is_port_free(host, preferred):
        return preferred, "preferred"
    if is_port_free(host, fallback):
        return fallback, "fallback"
    return fallback, "both-busy"


class Command(BaseCommand):
    help = (
        "Run the dashboard dev server on 7742, falling back to 4242 if 7742 is "
        "in use (prints the fallback). Pass an explicit port/addr to skip the fallback."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "addrport",
            nargs="?",
            default="",
            help=(
                "Optional bind target. A bare host (e.g. 0.0.0.0) keeps the "
                "7742->4242 auto-pick on that host; a port (9000) or addr:port "
                "(0.0.0.0:9000) is passed straight to runserver with no fallback."
            ),
        )
        # The runserver flags people actually reach for, forwarded verbatim.
        parser.add_argument("--noreload", action="store_true", help="Disable the auto-reloader.")
        parser.add_argument(
            "--nothreading", action="store_true", help="Run the dev server single-threaded."
        )
        parser.add_argument(
            "--insecure", action="store_true", help="Serve static files with DEBUG off."
        )

    def _runserver(self, addrport: str, options: dict[str, object]) -> None:
        """Invoke runserver, forwarding the passthrough flags we expose."""
        # runserver blocks forever and never flushes, so when stdout is a pipe
        # (not a terminal) our "falling back to 4242" line would sit unseen in
        # the block buffer. Flush it out before we hand off.
        self.stdout.flush()
        kwargs: dict[str, object] = {}
        if options.get("noreload"):
            kwargs["use_reloader"] = False
        if options.get("nothreading"):
            kwargs["use_threading"] = False
        if options.get("insecure"):
            kwargs["insecure_serving"] = True
        call_command("runserver", addrport, **kwargs)

    def handle(self, *args: object, **options: object) -> None:
        addrport = str(options.get("addrport") or "").strip()

        # An explicit port means the operator asked for a specific one; forward
        # it verbatim and don't second-guess with a fallback.
        if addrport and (":" in addrport or addrport.isdigit()):
            self.stdout.write(f"Starting dashboard on {addrport} (explicit).")
            self._runserver(addrport, options)
            return

        host = addrport or DEFAULT_HOST

        # Reloader re-exec: reuse the port already chosen, quietly.
        resolved = os.environ.get(_RESOLVED_PORT_ENV)
        if resolved and resolved.isdigit():
            self._runserver(f"{host}:{resolved}", options)
            return

        port, status = choose_port(host)
        if status == "fallback":
            self.stdout.write(
                self.style.WARNING(
                    f"Port {PREFERRED_PORT} is in use; falling back to {FALLBACK_PORT}."
                )
            )
        elif status == "both-busy":
            self.stdout.write(
                self.style.WARNING(
                    f"Ports {PREFERRED_PORT} and {FALLBACK_PORT} both look in use; "
                    f"trying {FALLBACK_PORT} anyway."
                )
            )
        self.stdout.write(self.style.SUCCESS(f"Starting dashboard on http://{host}:{port}"))
        os.environ[_RESOLVED_PORT_ENV] = str(port)
        self._runserver(f"{host}:{port}", options)
