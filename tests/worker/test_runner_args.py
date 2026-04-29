"""Tests for the worker runner's CLI argument parsing."""

from __future__ import annotations

import pytest

from franktheunicorn.worker.runner import _parse_args


class TestParseArgs:
    def test_default_log_level_is_none(self) -> None:
        args = _parse_args([])
        assert args.log_level is None

    def test_log_level_flag(self) -> None:
        args = _parse_args(["--log-level=DEBUG"])
        assert args.log_level == "DEBUG"

    def test_debug_shortcut_sets_debug(self) -> None:
        args = _parse_args(["--debug"])
        assert args.log_level == "DEBUG"

    def test_invalid_log_level_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--log-level=BANANAS"])

    def test_all_valid_levels_accepted(self) -> None:
        for level in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"):
            args = _parse_args([f"--log-level={level}"])
            assert args.log_level == level
