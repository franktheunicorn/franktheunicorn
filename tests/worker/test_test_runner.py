"""Tests for differential test verification with Docker (§9)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import ProjectConfig, TestExecutionConfig
from franktheunicorn.worker.test_runner import TestRunner as DockerTestRunner
from tests.factories import PullRequestFactory


def _enabled_config() -> ProjectConfig:
    """ProjectConfig with the runner enabled (default image, default tier)."""
    return ProjectConfig(owner="test", repo="test", tests=TestExecutionConfig(enabled=True))


def _ready_pr(**kwargs: object) -> object:
    """PR factory shortcut with SHAs populated."""
    defaults: dict[str, object] = {
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "body": "",
    }
    defaults.update(kwargs)
    return PullRequestFactory(**defaults)


@pytest.fixture
def fake_workspaces(tmp_path: Path):
    """Patch worktree context managers to yield ``tmp_path``."""
    pr_ws = tmp_path / "pr_ws"
    base_ws = tmp_path / "base_ws"
    pr_ws.mkdir()
    base_ws.mkdir()

    from contextlib import contextmanager

    @contextmanager
    def _pr_ctx(repo_path: Path, head_sha: str):
        yield pr_ws

    @contextmanager
    def _base_ctx(repo_path: Path, base_sha: str, head_sha: str, test_files: list[str]):
        yield base_ws

    with (
        patch("franktheunicorn.worker.test_runner.pr_branch_workspace", _pr_ctx),
        patch("franktheunicorn.worker.test_runner.base_cherry_pick_workspace", _base_ctx),
        patch(
            "franktheunicorn.worker.test_runner.resolve_image",
            return_value="python:3.12-slim",
        ),
    ):
        yield pr_ws, base_ws


@pytest.mark.django_db
class TestRunDifferential:
    def test_disabled_by_default(self) -> None:
        runner = DockerTestRunner()
        pr = _ready_pr(changed_files=["tests/test_main.py"])
        config = ProjectConfig(owner="test", repo="test")
        assert runner.run_differential_test(pr, config, Path("/repo")) is None

    def test_skips_when_no_test_files_and_not_ai(self, tmp_path: Path) -> None:
        runner = DockerTestRunner()
        pr = _ready_pr(changed_files=["src/main.py"], likely_ai_generated=False)
        assert runner.run_differential_test(pr, _enabled_config(), tmp_path) is None

    def test_skips_when_repo_path_missing(self) -> None:
        runner = DockerTestRunner()
        pr = _ready_pr(changed_files=["tests/test_main.py"])
        assert runner.run_differential_test(pr, _enabled_config(), None) is None
        assert runner.run_differential_test(pr, _enabled_config(), Path("/nonexistent")) is None

    def test_skips_when_shas_missing(self, tmp_path: Path) -> None:
        runner = DockerTestRunner()
        pr = PullRequestFactory(
            changed_files=["tests/test_main.py"], body="", head_sha="", base_sha=""
        )
        assert runner.run_differential_test(pr, _enabled_config(), tmp_path) is None

    def test_skips_untrusted_author_automatically(self, tmp_path: Path) -> None:
        runner = DockerTestRunner()
        pr = _ready_pr(changed_files=["tests/test_main.py"])
        # Default config has no committers/frequent_contributors and the
        # factory generates a random author — that author is untrusted.
        result = runner.run_differential_test(pr, _enabled_config(), tmp_path)
        assert result is None

    def test_force_bypasses_trusted_author_gate(
        self, tmp_path: Path, fake_workspaces: tuple[Path, Path]
    ) -> None:
        runner = DockerTestRunner()
        pr = _ready_pr(changed_files=["tests/test_main.py"])

        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b"1 passed"
        mock_docker.containers.run.return_value = mock_container

        with patch(
            "franktheunicorn.worker.test_runner.TestRunner._get_docker",
            return_value=mock_docker,
        ):
            result = runner.run_differential_test(pr, _enabled_config(), tmp_path, force=True)

        assert result is not None

    def test_runs_when_test_files_present(
        self, tmp_path: Path, fake_workspaces: tuple[Path, Path]
    ) -> None:
        runner = DockerTestRunner()
        pr = _ready_pr(changed_files=["src/main.py", "tests/test_main.py"])

        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b"1 passed"
        mock_docker.containers.run.return_value = mock_container

        with patch(
            "franktheunicorn.worker.test_runner.TestRunner._get_docker",
            return_value=mock_docker,
        ):
            result = runner.run_differential_test(pr, _enabled_config(), tmp_path, force=True)

        assert result is not None
        assert result.status == "completed"
        assert result.test_scope == ["tests/test_main.py"]
        # Two runs: PR + base.
        assert mock_docker.containers.run.call_count == 2

    def test_orphaned_running_testrun_does_not_block_reverification(
        self, tmp_path: Path, fake_workspaces: tuple[Path, Path]
    ) -> None:
        """A killed worker can leave a status="running" TestRun. That orphan
        must NOT count as "already tested" — the worker is restart-safe, so
        re-verification of the same head must still run."""
        from franktheunicorn.core.models import TestRun

        runner = DockerTestRunner()
        pr = _ready_pr(changed_files=["tests/test_main.py"], author="trusted-dev")
        config = ProjectConfig(
            owner="test",
            repo="test",
            tests=TestExecutionConfig(enabled=True),
            frequent_contributors=["trusted-dev"],
        )
        TestRun.objects.create(
            pull_request=pr, run_type="pr_branch", status="running", head_sha=pr.head_sha
        )

        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b"1 passed"
        mock_docker.containers.run.return_value = mock_container

        with patch(
            "franktheunicorn.worker.test_runner.TestRunner._get_docker",
            return_value=mock_docker,
        ):
            # Not force: the skip guard applies, but the orphaned running row
            # must not trigger it.
            result = runner.run_differential_test(pr, config, tmp_path)

        assert result is not None
        assert result.status == "completed"

    def test_completed_testrun_skips_reverification(self, tmp_path: Path) -> None:
        """A terminal (completed) run for the same head is skipped so
        unchanged PRs don't burn a container run every poll cycle."""
        from franktheunicorn.core.models import TestRun

        runner = DockerTestRunner()
        pr = _ready_pr(changed_files=["tests/test_main.py"], author="trusted-dev")
        config = ProjectConfig(
            owner="test",
            repo="test",
            tests=TestExecutionConfig(enabled=True),
            frequent_contributors=["trusted-dev"],
        )
        TestRun.objects.create(
            pull_request=pr, run_type="pr_branch", status="completed", head_sha=pr.head_sha
        )

        with patch("franktheunicorn.worker.test_runner.TestRunner._get_docker") as mock_get_docker:
            result = runner.run_differential_test(pr, config, tmp_path)

        assert result is None
        mock_get_docker.assert_not_called()  # never even reached docker

    def test_container_invocation_mounts_workspace(
        self, tmp_path: Path, fake_workspaces: tuple[Path, Path]
    ) -> None:
        pr_ws, _base_ws = fake_workspaces
        runner = DockerTestRunner()
        pr = _ready_pr(changed_files=["tests/test_main.py"])

        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b""
        mock_docker.containers.run.return_value = mock_container

        with patch(
            "franktheunicorn.worker.test_runner.TestRunner._get_docker",
            return_value=mock_docker,
        ):
            runner.run_differential_test(pr, _enabled_config(), tmp_path, force=True)

        first_call = mock_docker.containers.run.call_args_list[0]
        kwargs = first_call.kwargs
        assert kwargs["network_mode"] == "none"
        assert kwargs["read_only"] is True
        assert kwargs["working_dir"] == "/workspace"
        assert kwargs["volumes"] == {str(pr_ws): {"bind": "/workspace", "mode": "ro"}}
        assert "ALL" in kwargs["cap_drop"]
        # Writable tmpfs lives at a path *different* from the read-only repo
        # mount; mounting both at the same destination would break startup.
        assert "/workspace" not in kwargs["tmpfs"]
        assert "/frank-scratch" in kwargs["tmpfs"]
        assert kwargs["environment"]["HOME"] == "/frank-scratch"
        assert kwargs["environment"]["TMPDIR"] == "/frank-scratch"
        # tests.test_command renders {tests} into the command
        assert "pytest tests/test_main.py" in kwargs["command"][-1]

    def test_runs_for_ai_generated_prs(
        self, tmp_path: Path, fake_workspaces: tuple[Path, Path]
    ) -> None:
        runner = DockerTestRunner()
        pr = _ready_pr(changed_files=["src/main.py"], likely_ai_generated=True)

        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b"ok"
        mock_docker.containers.run.return_value = mock_container

        with patch(
            "franktheunicorn.worker.test_runner.TestRunner._get_docker",
            return_value=mock_docker,
        ):
            result = runner.run_differential_test(pr, _enabled_config(), tmp_path, force=True)

        assert result is not None

    def test_trusted_committer_runs_automatically(
        self, tmp_path: Path, fake_workspaces: tuple[Path, Path]
    ) -> None:
        runner = DockerTestRunner()
        config = ProjectConfig(
            owner="test",
            repo="test",
            committers=["trusted-person"],
            tests=TestExecutionConfig(enabled=True),
        )
        pr = _ready_pr(changed_files=["tests/test_main.py"], author="trusted-person")

        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b"ok"
        mock_docker.containers.run.return_value = mock_container

        with patch(
            "franktheunicorn.worker.test_runner.TestRunner._get_docker",
            return_value=mock_docker,
        ):
            result = runner.run_differential_test(pr, config, tmp_path)

        assert result is not None

    def test_handles_docker_unavailable(self, tmp_path: Path) -> None:
        runner = DockerTestRunner()
        pr = _ready_pr(changed_files=["tests/test_main.py"])
        with patch(
            "franktheunicorn.worker.test_runner.TestRunner._get_docker",
            return_value=None,
        ):
            assert runner.run_differential_test(pr, _enabled_config(), tmp_path, force=True) is None

    def test_exception_sets_failed(
        self, tmp_path: Path, fake_workspaces: tuple[Path, Path]
    ) -> None:
        runner = DockerTestRunner()
        pr = _ready_pr(changed_files=["tests/test_main.py"])

        mock_docker = MagicMock()

        with (
            patch(
                "franktheunicorn.worker.test_runner.TestRunner._get_docker",
                return_value=mock_docker,
            ),
            patch(
                "franktheunicorn.worker.test_runner.TestRunner._run_container",
                side_effect=RuntimeError("Docker exploded"),
            ),
        ):
            result = runner.run_differential_test(pr, _enabled_config(), tmp_path, force=True)

        assert result is not None
        assert result.status == "failed"
        assert "Docker exploded" in result.error_log


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

    def test_regression_verdict(self) -> None:
        """PR fails, base passes → broken (regression)."""
        runner = DockerTestRunner()
        assert (
            runner._compute_verdict(
                {"exit_code": 1, "stderr": "AssertionError"},
                {"exit_code": 0, "stderr": ""},
            )
            == "broken"
        )


class TestRunContainer:
    def test_run_container_timeout(self, tmp_path: Path) -> None:
        runner = DockerTestRunner()
        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.wait.side_effect = Exception("Container timed out waiting")
        mock_docker.containers.run.return_value = mock_container

        result = runner._run_container(
            mock_docker,
            "python:3.12-slim",
            tmp_path,
            ["tests/test_main.py"],
            {"timeout": 10},
            TestExecutionConfig(enabled=True),
        )

        assert result["timed_out"] is True
        assert result["exit_code"] == -1

    def test_run_container_non_timeout_exception(self, tmp_path: Path) -> None:
        runner = DockerTestRunner()
        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.wait.side_effect = Exception("OOM killed")
        mock_docker.containers.run.return_value = mock_container

        with pytest.raises(Exception, match="OOM killed"):
            runner._run_container(
                mock_docker,
                "python:3.12-slim",
                tmp_path,
                ["tests/test_main.py"],
                {"timeout": 10},
                TestExecutionConfig(enabled=True),
            )

    def test_run_container_uses_test_command_template(self, tmp_path: Path) -> None:
        runner = DockerTestRunner()
        mock_docker = MagicMock()
        mock_container = MagicMock()
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b""
        mock_docker.containers.run.return_value = mock_container

        cfg = TestExecutionConfig(
            enabled=True,
            test_command="go test {tests}",
            workdir="/src",
            env={"FOO": "bar"},
        )
        runner._run_container(
            mock_docker,
            "golang:1.22",
            tmp_path,
            ["./pkg/foo"],
            {"timeout": 10},
            cfg,
        )
        kwargs = mock_docker.containers.run.call_args.kwargs
        assert kwargs["command"][-1] == "go test ./pkg/foo"
        assert kwargs["working_dir"] == "/src"
        # User env merges with the scratch defaults; user keys win on collision.
        assert kwargs["environment"]["FOO"] == "bar"
        assert kwargs["environment"]["HOME"] == "/frank-scratch"
