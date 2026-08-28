"""Tests for loading operator config off disk.

The theme is that a config problem must not present as a *feature* problem.
Everything here degrades to OperatorConfig() defaults, and a default is never the
thing the operator wrote — so silence turns "your YAML is broken" into "the thing I
configured isn't happening", which is a much harder bug to find.

Note what changed on 2026-08-28 and what didn't. ``security_triage.enabled`` and
``verifier.enabled`` now default *true*, so the original reported symptom —
"security_triage.enabled is false while my file says true" — is no longer reachable
that way. The trap itself is untouched: the fallback still discards backends,
reviewer entries, credentials and any ``false`` the operator set, and it still does
so for a reason recorded nowhere near the feature that stops working. So these
tests moved off the flag and onto values a default cannot coincide with.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from franktheunicorn.config.loader import load_operator_config


class TestOperatorConfigFailuresAreLoud:
    """Every failure path here degrades to OperatorConfig() defaults, and a default
    is not what the operator asked for. So a problem anywhere in the file presents
    as "the thing I configured isn't happening", with nothing connecting the two.
    """

    def test_a_bad_value_elsewhere_silently_discarded_everything(
        self, tmp_path: Path, caplog: Any
    ) -> None:
        """The reported symptom, in its surviving form. One unrelated bad key and
        the whole file is gone — so the log has to name the file, the field, and
        the fallback.

        Asserted on ``agent_cli_reviewers`` rather than on a flag: the flags now
        default to what this file asks for, which makes them useless as evidence
        that the file was read. A named reviewer entry cannot arrive by accident.
        """
        config = tmp_path / "operator.yaml"
        config.write_text(
            "agent_cli_reviewers:\n"
            "  - name: 'my-wrapper'\n"
            "    cli_path: 'corp-wrap claude'\n"
            "poll_interval_seconds: 'not a number'\n"
        )

        with caplog.at_level(logging.ERROR):
            loaded = load_operator_config(config)

        assert not any(rc.name == "my-wrapper" for rc in loaded.agent_cli_reviewers)  # the trap
        assert "poll_interval_seconds" in caplog.text
        assert "defaults are in force" in caplog.text
        assert str(config) in caplog.text

    def test_an_operator_who_turned_something_off_gets_it_back_on(
        self, tmp_path: Path, caplog: Any
    ) -> None:
        """The default flip makes the fallback worse in one specific direction, and
        it is worth pinning: an operator who deliberately switched the verifier off
        and then broke an unrelated key gets it silently switched back on, because
        "back to defaults" now means "back to on"."""
        config = tmp_path / "operator.yaml"
        config.write_text(
            "security_triage:\n  verifier:\n    enabled: false\npoll_interval_seconds: 'nope'\n"
        )

        with caplog.at_level(logging.ERROR):
            loaded = load_operator_config(config)

        assert loaded.security_triage.verifier.enabled is True
        assert "defaults are in force" in caplog.text

    def test_unparseable_yaml_says_everything_was_ignored(
        self, tmp_path: Path, caplog: Any
    ) -> None:
        config = tmp_path / "operator.yaml"
        config.write_text("llm_backends:\n  - name: mine\n  bad: [unclosed\n")

        with caplog.at_level(logging.ERROR):
            loaded = load_operator_config(config)

        assert loaded.llm_backends == []
        assert "defaults are in force" in caplog.text

    def test_a_block_nested_too_shallow_is_named(self, tmp_path: Path, caplog: Any) -> None:
        """Pydantic's extra="ignore" drops it without a word. `verifier:` belongs
        under `security_triage:`, and putting it at the top level is the easy
        mistake — now doubly worth naming, because the setting it is trying to
        change is one whose default is True, so getting it wrong in the "off"
        direction produces no visible symptom at all."""
        config = tmp_path / "operator.yaml"
        config.write_text("security_triage:\n  enabled: true\nverifier:\n  enabled: false\n")

        with caplog.at_level(logging.WARNING):
            loaded = load_operator_config(config)

        assert loaded.security_triage.verifier.enabled is True  # the top-level block did nothing
        assert "verifier" in caplog.text
        assert "indentation" in caplog.text

    def test_a_misspelled_top_level_key_is_named(self, tmp_path: Path, caplog: Any) -> None:
        config = tmp_path / "operator.yaml"
        config.write_text("secuirty_triage:\n  enabled: true\n")

        with caplog.at_level(logging.WARNING):
            load_operator_config(config)

        assert "secuirty_triage" in caplog.text

    def test_a_correct_file_logs_nothing_and_works(self, tmp_path: Path, caplog: Any) -> None:
        config = tmp_path / "operator.yaml"
        config.write_text(
            "security_triage:\n  enabled: true\n  verifier:\n    enabled: true\n"
            "    reviewer: 'cursor-agent'\n"
        )

        with caplog.at_level(logging.WARNING):
            loaded = load_operator_config(config)

        assert loaded.security_triage.enabled is True
        assert loaded.security_triage.verifier.enabled is True
        assert loaded.security_triage.verifier.reviewer == "cursor-agent"
        assert caplog.text == ""

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path, caplog: Any) -> None:
        """Absent is the documented default-install case, not a misconfiguration."""
        with caplog.at_level(logging.WARNING):
            loaded = load_operator_config(tmp_path / "nope.yaml")

        assert caplog.text == ""
        # And absent now means triage is *on*, which is the whole point of the flip:
        # the install our own docs produce (no operator.yaml at all, or the shipped
        # one with the block commented out) is the install that should work.
        assert loaded.security_triage.enabled is True
        assert loaded.security_triage.verifier.enabled is True


class TestSecurityDefaults:
    """The defaults themselves, pinned. These are a decision, not an accident, and
    a future edit that flips one should have to change a test that says why."""

    def test_triage_and_verification_are_on_out_of_the_box(self) -> None:
        from franktheunicorn.config.models import OperatorConfig

        triage = OperatorConfig().security_triage
        assert triage.enabled is True
        assert triage.auto_triage is True
        assert triage.verifier.enabled is True

    def test_poc_execution_and_the_email_poller_stay_off(self) -> None:
        """The two exceptions, and the reasons they are exceptions: sandbox_enabled
        runs proof-of-concept code a stranger emailed you, and the email poller
        cannot work without IMAP credentials so defaulting it on would only produce
        a connection attempt to nowhere every cycle."""
        from franktheunicorn.config.models import OperatorConfig

        triage = OperatorConfig().security_triage
        assert triage.sandbox_enabled is False
        assert triage.email.enabled is False


class TestNonMappingConfig:
    """Valid YAML of the wrong shape. `OperatorConfig(**data)` raises TypeError for a
    list/str/int, which is not one of the exceptions the loader catches — so it
    escaped the degrade-to-defaults contract the docstring promises, and took
    `manage.py show_config` down with a traceback on exactly the configs that
    command exists to diagnose."""

    @pytest.mark.parametrize(
        ("label", "text"),
        [
            ("a list", "- security_triage\n- llm_backends\n"),
            ("a bare string", "just some prose someone left here\n"),
            ("a number", "42\n"),
        ],
    )
    def test_it_degrades_instead_of_raising(
        self, tmp_path: Path, caplog: Any, label: str, text: str
    ) -> None:
        config = tmp_path / "operator.yaml"
        config.write_text(text)

        with caplog.at_level(logging.ERROR):
            loaded = load_operator_config(config)

        assert loaded.security_triage.enabled is True  # i.e. defaults, not a crash
        assert "not a mapping of settings" in caplog.text
        assert str(config) in caplog.text

    def test_the_message_names_the_likely_cause(self, tmp_path: Path, caplog: Any) -> None:
        """A leading "- " on the first line is how a hand-edited config becomes a
        list, and it is not obvious from the symptom."""
        config = tmp_path / "operator.yaml"
        config.write_text("- github_username: holdenk\n")

        with caplog.at_level(logging.ERROR):
            load_operator_config(config)

        assert "leading `- `" in caplog.text

    def test_show_config_survives_it(self, tmp_path: Path) -> None:
        import io

        from django.core.management import call_command

        config = tmp_path / "operator.yaml"
        config.write_text("- one\n- two\n")

        out = io.StringIO()
        call_command("show_config", "--path", str(config), stdout=out, stderr=out)

        assert "not a mapping" in out.getvalue()
