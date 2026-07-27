"""Tests for the RLM notebook sandbox runner (no real Docker)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig, RLMConfig
from franktheunicorn.review.rlm import sandbox_runner
from franktheunicorn.review.rlm.protocol import BrokerClient
from franktheunicorn.review.rlm.sandbox_runner import (
    RLMSandboxUnavailableError,
    _container_command,
    run_rlm_notebook,
)


def _payload() -> dict[str, object]:
    return {
        "diff": "+++ b/a.py\n",
        "pr": {"number": 1},
        "files": {},
        "anti_patterns": [],
        "tone": "",
    }


def _config() -> RLMConfig:
    return RLMConfig(
        execution="notebook", image="frank-rlm:test", container_timeout=30, max_model_calls=5
    )


def test_container_command_is_hardened() -> None:
    cmd = _container_command("frank-rlm:test", Path("/work"), None)
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "--network=none" in cmd
    assert "--read-only" in cmd
    assert "/work:/rlm" in cmd
    assert cmd[-3:] == ["jupyter", "execute", "/rlm/review.ipynb"]
    assert "frank-rlm:test" in cmd


def test_container_command_mounts_repo(tmp_path: Path) -> None:
    cmd = _container_command("img", Path("/work"), tmp_path)
    assert f"{tmp_path}:/repo:ro" in cmd


def test_unavailable_without_docker() -> None:
    with patch.object(sandbox_runner, "_docker_available", return_value=False):  # noqa: SIM117
        with pytest.raises(RLMSandboxUnavailableError):
            run_rlm_notebook(
                _payload(), {"stub": LLMBackendConfig(provider="stub")}, config=_config()
            )


def test_unavailable_without_models() -> None:
    with patch.object(sandbox_runner, "_docker_available", return_value=True):  # noqa: SIM117
        with pytest.raises(RLMSandboxUnavailableError):
            run_rlm_notebook(_payload(), {}, config=_config())


def test_full_run_collects_findings_via_broker() -> None:
    """Simulate the container: a fake `docker run` connects to the live broker
    socket and drives it exactly as the notebook would."""

    def fake_docker_run(cmd, capture_output, text, timeout):
        workdir = next(a.split(":", 1)[0] for a in cmd if a.endswith(":/rlm"))
        client = BrokerClient(str(Path(workdir) / "broker.sock"))
        assert "stub" in client.models()
        client.llm("review the diff", model="stub")  # recursive self-call
        client.emit({"file_path": "a.py", "body": "found a bug", "line": 3, "severity": "nit"})
        client.log("done")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    with (
        patch.object(sandbox_runner, "_docker_available", return_value=True),
        patch.object(sandbox_runner.subprocess, "run", side_effect=fake_docker_run),
    ):
        result = run_rlm_notebook(
            _payload(),
            {"stub": LLMBackendConfig(provider="stub")},
            config=_config(),
        )

    assert result.returncode == 0
    assert len(result.findings) == 1
    assert result.findings[0].file_path == "a.py"
    assert "1 finding" in result.overall_vibe
    assert "done" in result.log


def test_timeout_returns_partial(tmp_path: Path) -> None:
    import subprocess

    def boom(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="docker", timeout=30)

    with (
        patch.object(sandbox_runner, "_docker_available", return_value=True),
        patch.object(sandbox_runner.subprocess, "run", side_effect=boom),
    ):
        result = run_rlm_notebook(
            _payload(), {"stub": LLMBackendConfig(provider="stub")}, config=_config()
        )
    assert result.returncode is None
    assert "timed out" in result.log
