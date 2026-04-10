"""Tests for sandbox POC execution."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from franktheunicorn.security.sandbox import SandboxResult, _docker_available, run_poc_in_sandbox


class TestSandboxResult:
    def test_fields(self) -> None:
        r = SandboxResult(verdict="confirmed", output="hello", exit_code=0)
        assert r.verdict == "confirmed"
        assert r.output == "hello"
        assert r.exit_code == 0

    def test_defaults(self) -> None:
        r = SandboxResult(verdict="error", output="oops")
        assert r.exit_code is None


class TestRunPocInSandbox:
    def test_empty_poc_returns_error(self) -> None:
        report = MagicMock()
        report.parsed_poc = "   "
        result = run_poc_in_sandbox(report)
        assert result.verdict == "error"
        assert "No POC" in result.output

    @patch("franktheunicorn.security.sandbox._docker_available", return_value=False)
    def test_no_docker_returns_error(self, mock_docker: MagicMock) -> None:
        report = MagicMock()
        report.parsed_poc = "echo hello"
        result = run_poc_in_sandbox(report)
        assert result.verdict == "error"
        assert "Docker" in result.output

    @patch("franktheunicorn.security.sandbox.subprocess.run")
    @patch("franktheunicorn.security.sandbox._docker_available", return_value=True)
    def test_successful_poc_returns_confirmed(
        self, mock_docker: MagicMock, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="POC output", stderr="")

        report = MagicMock()
        report.parsed_poc = "echo vulnerable"
        report.pk = 1

        result = run_poc_in_sandbox(report)
        assert result.verdict == "confirmed"
        assert result.exit_code == 0
        assert "POC output" in result.output

    @patch("franktheunicorn.security.sandbox.subprocess.run")
    @patch("franktheunicorn.security.sandbox._docker_available", return_value=True)
    def test_failed_poc_returns_not_reproduced(
        self, mock_docker: MagicMock, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Error: not found")

        report = MagicMock()
        report.parsed_poc = "exploit_script"
        report.pk = 2

        result = run_poc_in_sandbox(report)
        assert result.verdict == "not-reproduced"
        assert result.exit_code == 1
        assert "stderr" in result.output

    @patch("franktheunicorn.security.sandbox.subprocess.run")
    @patch("franktheunicorn.security.sandbox._docker_available", return_value=True)
    def test_timeout_returns_error(self, mock_docker: MagicMock, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=60)

        report = MagicMock()
        report.parsed_poc = "sleep 999"
        report.pk = 3

        result = run_poc_in_sandbox(report)
        assert result.verdict == "error"
        assert "timed out" in result.output

    @patch("franktheunicorn.security.sandbox.subprocess.run")
    @patch("franktheunicorn.security.sandbox._docker_available", return_value=True)
    def test_exception_returns_error(self, mock_docker: MagicMock, mock_run: MagicMock) -> None:
        mock_run.side_effect = OSError("Docker crashed")

        report = MagicMock()
        report.parsed_poc = "echo test"
        report.pk = 4

        result = run_poc_in_sandbox(report)
        assert result.verdict == "error"
        assert "Sandbox execution failed" in result.output

    @patch("franktheunicorn.security.sandbox.subprocess.run")
    @patch("franktheunicorn.security.sandbox._docker_available", return_value=True)
    def test_repo_path_mounted(
        self, mock_docker: MagicMock, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        report = MagicMock()
        report.parsed_poc = "ls /repo"
        report.pk = 5

        run_poc_in_sandbox(report, repo_path=tmp_path)

        # Verify the docker command included the repo mount.
        call_args = mock_run.call_args[0][0]
        assert any(str(tmp_path) in arg for arg in call_args)


class TestDockerAvailable:
    @patch("franktheunicorn.security.sandbox.subprocess.run")
    def test_available(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        assert _docker_available() is True

    @patch("franktheunicorn.security.sandbox.subprocess.run")
    def test_not_available(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        assert _docker_available() is False

    @patch("franktheunicorn.security.sandbox.subprocess.run")
    def test_not_installed(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError
        assert _docker_available() is False

    @patch("franktheunicorn.security.sandbox.subprocess.run")
    def test_timeout(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)
        assert _docker_available() is False
