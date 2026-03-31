"""Tests for differential test verification with Docker (§9)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.worker.test_runner import TestRunner as DockerTestRunner
from tests.factories import PullRequestFactory


@pytest.mark.django_db
class TestTestRunner:
    def test_skips_when_no_test_files_and_not_ai(self) -> None:
        runner = DockerTestRunner()
        pr = PullRequestFactory(changed_files=["src/main.py"], likely_ai_generated=False)

        from franktheunicorn.config.models import ProjectConfig

        config = ProjectConfig(owner="test", repo="test")
        result = runner.run_differential_test(pr, config)
        assert result is None

    def test_runs_when_test_files_present(self) -> None:
        runner = DockerTestRunner()
        pr = PullRequestFactory(
            changed_files=["src/main.py", "tests/test_main.py"],
            body="",
        )

        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b"1 passed"
        mock_docker.containers.run.return_value = mock_container
        mock_docker.ping.return_value = True

        with patch(
            "franktheunicorn.worker.test_runner.TestRunner._get_docker", return_value=mock_docker
        ):
            from franktheunicorn.config.models import ProjectConfig

            config = ProjectConfig(owner="test", repo="test")
            result = runner.run_differential_test(pr, config)

        assert result is not None
        assert result.status == "completed"
        assert result.test_scope == ["tests/test_main.py"]

    def test_runs_for_ai_generated_prs(self) -> None:
        runner = DockerTestRunner()
        pr = PullRequestFactory(
            changed_files=["src/main.py"],
            likely_ai_generated=True,
            body="",
        )

        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b"ok"
        mock_docker.containers.run.return_value = mock_container
        mock_docker.ping.return_value = True

        with patch(
            "franktheunicorn.worker.test_runner.TestRunner._get_docker", return_value=mock_docker
        ):
            from franktheunicorn.config.models import ProjectConfig

            config = ProjectConfig(owner="test", repo="test")
            result = runner.run_differential_test(pr, config)

        assert result is not None

    def test_handles_docker_unavailable(self) -> None:
        runner = DockerTestRunner()
        pr = PullRequestFactory(
            changed_files=["tests/test_main.py"],
            body="",
        )

        with patch("franktheunicorn.worker.test_runner.TestRunner._get_docker", return_value=None):
            from franktheunicorn.config.models import ProjectConfig

            config = ProjectConfig(owner="test", repo="test")
            result = runner.run_differential_test(pr, config)

        assert result is None


class TestComputeVerdict:
    def test_good_verdict(self) -> None:
        runner = DockerTestRunner()
        assert (
            runner._compute_verdict(
                {"exit_code": 0, "stderr": ""},
                {"exit_code": 1, "stderr": "AssertionError"},
            )
            == "good"
        )

    def test_suspect_verdict(self) -> None:
        runner = DockerTestRunner()
        assert (
            runner._compute_verdict(
                {"exit_code": 0, "stderr": ""},
                {"exit_code": 0, "stderr": ""},
            )
            == "suspect"
        )

    def test_broken_verdict(self) -> None:
        runner = DockerTestRunner()
        assert (
            runner._compute_verdict(
                {"exit_code": 1, "stderr": ""},
                {"exit_code": 1, "stderr": ""},
            )
            == "broken"
        )

    def test_infra_verdict(self) -> None:
        runner = DockerTestRunner()
        assert (
            runner._compute_verdict(
                {"exit_code": 0, "stderr": ""},
                {"exit_code": 1, "stderr": "ImportError: No module named 'foo'"},
            )
            == "infra"
        )
