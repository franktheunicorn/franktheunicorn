"""Tests for the worker runner's CLI argument parsing."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from franktheunicorn.worker import runner as runner_module
from franktheunicorn.worker.runner import _parse_args, run_worker


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


class TestRunWorkerArgvHandling:
    """Regression tests: ``run_worker()`` must not consume ``sys.argv``.

    The Django ``run_worker`` management command calls ``run_worker()`` from
    inside ``manage.py``, where ``sys.argv`` contains tokens like
    ``manage.py`` and ``run_worker``. If argparse falls back to ``sys.argv``
    those tokens trigger ``SystemExit(2)`` before the worker can start.
    """

    def test_default_argv_does_not_consume_sys_argv(self) -> None:
        # Simulate the manage.py invocation: sys.argv looks nothing like a
        # valid argument list for the worker parser.
        sentinel = ["manage.py", "run_worker", "--verbosity=2"]
        # Stop run_worker before it actually starts polling — we only care
        # that argument parsing succeeds.
        marker = RuntimeError("reached django.setup")
        with (
            patch.object(sys, "argv", sentinel),
            patch.object(runner_module, "django") as mock_django,
        ):
            mock_django.setup.side_effect = marker
            with pytest.raises(RuntimeError, match=r"reached django\.setup"):
                run_worker()
