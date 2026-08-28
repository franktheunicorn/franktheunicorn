"""Tests for loading operator config off disk.

The theme is that a config problem must not present as a *feature* problem.
Everything here degrades to OperatorConfig() defaults, and those have every
optional feature off — so silence turns "your YAML is broken" into "the thing you
enabled is disabled", which is a much harder bug to find.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from franktheunicorn.config.loader import load_operator_config


class TestOperatorConfigFailuresAreLoud:
    """Every failure path here degrades to OperatorConfig() defaults, and the
    defaults have every optional feature *off*. So a problem anywhere in the file
    presents as "the feature you enabled is disabled", with nothing connecting the
    two — which is exactly how an operator ends up reading
    "security_triage.enabled is false" while looking at `enabled: true`.
    """

    def test_a_bad_value_elsewhere_silently_disabled_everything(
        self, tmp_path: Path, caplog: Any
    ) -> None:
        """The reported symptom. One unrelated bad key, and the feature you turned
        on is off — so the log has to name the file, the field, and the fallback."""
        config = tmp_path / "operator.yaml"
        config.write_text(
            "security_triage:\n  enabled: true\npoll_interval_seconds: 'not a number'\n"
        )

        with caplog.at_level(logging.ERROR):
            loaded = load_operator_config(config)

        assert loaded.security_triage.enabled is False  # the trap
        assert "poll_interval_seconds" in caplog.text
        assert "defaults are in force" in caplog.text
        assert str(config) in caplog.text

    def test_unparseable_yaml_says_everything_was_ignored(
        self, tmp_path: Path, caplog: Any
    ) -> None:
        config = tmp_path / "operator.yaml"
        config.write_text("security_triage:\n  enabled: true\n  bad: [unclosed\n")

        with caplog.at_level(logging.ERROR):
            loaded = load_operator_config(config)

        assert loaded.security_triage.enabled is False
        assert "defaults are in force" in caplog.text

    def test_a_block_nested_too_shallow_is_named(self, tmp_path: Path, caplog: Any) -> None:
        """Pydantic's extra="ignore" drops it without a word, and the only symptom
        is a feature that won't turn on. `verifier:` belongs under
        `security_triage:`, and putting it at the top level is the easy mistake."""
        config = tmp_path / "operator.yaml"
        config.write_text("security_triage:\n  enabled: true\nverifier:\n  enabled: true\n")

        with caplog.at_level(logging.WARNING):
            loaded = load_operator_config(config)

        assert loaded.security_triage.enabled is True
        assert loaded.security_triage.verifier.enabled is False
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
        config.write_text("security_triage:\n  enabled: true\n  verifier:\n    enabled: true\n")

        with caplog.at_level(logging.WARNING):
            loaded = load_operator_config(config)

        assert loaded.security_triage.enabled is True
        assert loaded.security_triage.verifier.enabled is True
        assert caplog.text == ""

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path, caplog: Any) -> None:
        """Absent is the documented default-install case, not a misconfiguration."""
        with caplog.at_level(logging.WARNING):
            loaded = load_operator_config(tmp_path / "nope.yaml")

        assert loaded.security_triage.enabled is False
        assert caplog.text == ""
