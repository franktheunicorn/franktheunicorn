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

    from franktheunicorn.config.models import OperatorConfig


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
                    "default. Security triage and verification default on; everything "
                    "needing a credential or a CLI (llm_backends, agent_cli_reviewers, "
                    "the email inbox) has nothing to work with."
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
                    "  Nothing in it is being used — every setting is at its built-in "
                    "default, including your backends and credentials, which have none."
                )
            )
            return
        if not isinstance(data, dict):
            # Valid YAML, wrong shape. Printed rather than left to the loader's log
            # line, because this command's whole job is telling somebody what is wrong
            # with their file and they are reading stdout, not the worker log.
            self.stdout.write(
                self.style.ERROR(
                    f"  This file is valid YAML but parses as {type(data).__name__}, not a "
                    "mapping of settings, so every setting is at its built-in default. It "
                    "should be top-level `key: value` pairs — a leading `- ` on the first "
                    "line makes the whole document a list."
                )
            )
            return
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
            # "(unset)" belongs in the not-OK set: this is the one command whose job
            # is spotting a setting that isn't there, and it was painting an unset
            # verifier.reviewer green.
            flag = (
                self.style.SUCCESS
                if value not in (False, 0, "0 configured", "(unset)", "")
                else self.style.WARNING
            )
            self.stdout.write(f"  {label:38} {flag(str(value))}")

        # The specific gate the security features check, spelled out, because
        # "enabled: true" in the file and "enabled" in force are different claims
        # and this command exists to tell them apart.
        #
        # Both flags now default *true*, which inverts what a False here means: it
        # is no longer "you never turned it on", it is "something turned it off",
        # and the most likely something is an unrelated validation error that sent
        # the whole file back to defaults. Except that defaults are on, so the
        # remaining causes are a real `false` in the file or a stale file — and
        # those are the two this text now points at.
        if not triage.enabled:
            self.stdout.write(
                self.style.WARNING(
                    "\nsecurity_triage.enabled is FALSE in force, and it defaults TRUE. "
                    "So something set it: either this file says false, or this is not "
                    "the file you edited (compare with --path)."
                )
            )
        elif not verifier.enabled:
            self.stdout.write(
                self.style.WARNING(
                    "\nsecurity_triage.verifier.enabled is FALSE in force, and it "
                    "defaults TRUE — so this file, or a stale copy of it, sets it false."
                )
            )
        elif not self._verifier_reviewer_resolves(config):
            # The one thing left that can stop verification on an otherwise-fine
            # config, now that neither flag needs setting. Worth naming here rather
            # than leaving to a warning in the worker log.
            names = ", ".join(rc.name for rc in config.agent_cli_reviewers) or "none"
            self.stdout.write(
                self.style.WARNING(
                    f"\nTriage and verification are both on, but "
                    f"security_triage.verifier.reviewer={verifier.reviewer!r} matches no "
                    f"agent_cli_reviewers entry (configured: {names}), so there is no "
                    "coding-agent CLI to run. Verification cannot work until that "
                    "resolves."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nTriage and verification are both on."))

    @staticmethod
    def _verifier_reviewer_resolves(config: OperatorConfig) -> bool:
        from franktheunicorn.security.verifier import resolve_verifier_reviewer

        return resolve_verifier_reviewer(config, config.security_triage.verifier) is not None
