"""Print which operator.yaml is in force and what it actually resolved to.

Exists because "the feature I enabled is reported as disabled" has three causes
that look identical from the dashboard — a different file than the one you edited,
a block nested one level too shallow, and an unrelated validation error that sent
the *whole* config back to defaults — and none of them is visible without reading
the worker log at the right moment.

Run it in the same place the worker runs (same container, same shell, same
FRANK_OPERATOR_CONFIG), because that is the question it answers.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from django.conf import settings
from django.core.management.base import BaseCommand

from franktheunicorn.config.loader import load_operator_config

if TYPE_CHECKING:
    from django.core.management.base import CommandParser


class Command(BaseCommand):
    help = "Show the operator config path in force and the flags it resolved to"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--path",
            help="Check this file instead of the one settings resolved (for comparing two)",
        )

    def handle(self, *args: object, **options: object) -> None:
        raw_path = options.get("path")
        path = Path(str(raw_path)) if raw_path else Path(settings.FRANK_OPERATOR_CONFIG)

        self.stdout.write(self.style.MIGRATE_HEADING("Config file"))
        self.stdout.write(f"  path    : {path}")
        self.stdout.write(f"  exists  : {path.exists()}")
        if not path.exists():
            self.stdout.write(
                self.style.WARNING(
                    "  This file does not exist, so every setting is at its built-in "
                    "default — which means every optional feature is off."
                )
            )
            return

        # The keys as YAML sees them, before the model gets a chance to drop any.
        # A block indented one level too shallow shows up here at the top level,
        # which is the fastest way to spot it.
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            self.stdout.write(self.style.ERROR(f"  This file is not valid YAML: {exc}"))
            self.stdout.write(
                self.style.ERROR(
                    "  Nothing in it is being used. Every feature is at its default (off)."
                )
            )
            return
        if isinstance(data, dict):
            self.stdout.write(f"  top-level keys: {', '.join(sorted(data)) or '(none)'}")

        # Through the real loader, so anything it warns about is printed here too.
        config = load_operator_config(path)
        triage = config.security_triage
        verifier = triage.verifier

        self.stdout.write(self.style.MIGRATE_HEADING("\nResolved (what the code will actually do)"))
        for label, value in (
            ("security_triage.enabled", triage.enabled),
            ("security_triage.auto_triage", triage.auto_triage),
            ("security_triage.sandbox_enabled", triage.sandbox_enabled),
            ("security_triage.verifier.enabled", verifier.enabled),
            ("security_triage.verifier.reviewer", verifier.reviewer or "(unset)"),
            ("llm_backends", f"{len(config.llm_backends)} configured"),
            ("agent_cli_reviewers", f"{len(config.agent_cli_reviewers)} configured"),
        ):
            flag = (
                self.style.SUCCESS
                if value not in (False, 0, "0 configured")
                else self.style.WARNING
            )
            self.stdout.write(f"  {label:38} {flag(str(value))}")

        # The specific gate the security features check, spelled out, because
        # "enabled: true" in the file and "enabled" in force are different claims
        # and this command exists to tell them apart.
        if not triage.enabled:
            self.stdout.write(
                self.style.WARNING(
                    "\nsecurity_triage.enabled is FALSE in force. If your file says true, "
                    "then either this is not the file you edited (compare with --path), a "
                    "block is nested one level too shallow, or something else in the file "
                    "failed validation and sent the whole config back to defaults — the "
                    "loader logs which, at ERROR."
                )
            )
        elif not verifier.enabled:
            self.stdout.write(
                self.style.WARNING(
                    "\nVerification is off. Add `verifier: {enabled: true}` *under* "
                    "security_triage — at the top level it is silently ignored."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nTriage and verification are both on."))
