"""Tests for ``franktheunicorn.env_loader``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from franktheunicorn.env_loader import load_dotenv, load_project_dotenv


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("FRANK_TEST_A", "FRANK_TEST_B", "FRANK_TEST_C", "FRANK_TEST_D", "FRANK_TEST_DUP"):
        monkeypatch.delenv(key, raising=False)


def test_load_dotenv_missing_file_is_noop(tmp_path: Path, clean_env: None) -> None:
    load_dotenv(tmp_path / "does-not-exist.env")
    assert "FRANK_TEST_A" not in os.environ


def test_load_dotenv_parses_keys_and_strips_quotes(
    tmp_path: Path,
    clean_env: None,
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "# comment line",
                "",
                "FRANK_TEST_A=plain",
                'FRANK_TEST_B="double-quoted"',
                "FRANK_TEST_C='single-quoted'",
                "export FRANK_TEST_D=exported",
                "INVALID_LINE_NO_EQUALS",
                "=novalue",
            ]
        )
    )
    load_dotenv(env)
    assert os.environ["FRANK_TEST_A"] == "plain"
    assert os.environ["FRANK_TEST_B"] == "double-quoted"
    assert os.environ["FRANK_TEST_C"] == "single-quoted"
    assert os.environ["FRANK_TEST_D"] == "exported"


def test_load_dotenv_does_not_override_existing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRANK_TEST_A", "from-shell")
    env = tmp_path / ".env"
    env.write_text("FRANK_TEST_A=from-file\n")
    load_dotenv(env)
    assert os.environ["FRANK_TEST_A"] == "from-shell"


def test_load_dotenv_last_value_wins_within_file(
    tmp_path: Path,
    clean_env: None,
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "FRANK_TEST_DUP=",
                "# FRANK_TEST_DUP=commented-out",
                "FRANK_TEST_DUP=real-token",
            ]
        )
    )
    load_dotenv(env)
    assert os.environ["FRANK_TEST_DUP"] == "real-token"


def test_load_project_dotenv_walks_up_to_pyproject(
    tmp_path: Path,
    clean_env: None,
) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / ".env").write_text("FRANK_TEST_A=found\n")
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    load_project_dotenv(start=nested / "module.py")
    assert os.environ["FRANK_TEST_A"] == "found"


def test_load_project_dotenv_no_marker_is_noop(
    tmp_path: Path,
    clean_env: None,
) -> None:
    nested = tmp_path / "deep" / "dir"
    nested.mkdir(parents=True)
    load_project_dotenv(start=nested / "module.py")
    assert "FRANK_TEST_A" not in os.environ
