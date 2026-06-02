"""Tests for agent-tool wiring in the review drafter."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import (
    AgentToolsConfig,
    LLMBackendConfig,
    ProjectConfig,
)
from franktheunicorn.review import drafter
from franktheunicorn.review.agent_tools import anthropic_tool_specs, build_tool_registry
from franktheunicorn.review.backends.base import BaseLLMBackend
from franktheunicorn.worker.tool_sandbox import ToolCommandResult
from tests.conftest import make_pr_context
from tests.factories import PullRequestFactory


class FakeRunner:
    def __init__(self, available: set[str]):
        self._available = available

    def exec(self, argv, *, cwd="/workspace", timeout=None):
        return ToolCommandResult(0, "", "", False)

    def tool_available(self, binary: str) -> bool:
        return binary in self._available


@pytest.mark.django_db
class TestToolSession:
    def test_disabled_yields_nothing_and_skips_docker(self) -> None:
        cfg = ProjectConfig(owner="o", repo="r")  # agent_tools off by default
        pr = PullRequestFactory(head_sha="a" * 40)
        with patch.object(drafter, "_get_docker_client") as mock_docker:
            with drafter._tool_session(pr, cfg, Path("/repo")) as (runner, registry, specs):
                assert runner is None
                assert registry == {}
                assert specs == []
            mock_docker.assert_not_called()

    def test_no_head_sha_yields_nothing(self) -> None:
        cfg = ProjectConfig(owner="o", repo="r", agent_tools=AgentToolsConfig(enabled=True))
        pr = PullRequestFactory(head_sha="")
        with patch.object(drafter, "_get_docker_client") as mock_docker:
            with drafter._tool_session(pr, cfg, Path("/repo")) as (runner, _r, _s):
                assert runner is None
            mock_docker.assert_not_called()

    def test_docker_unavailable_falls_back(self) -> None:
        cfg = ProjectConfig(owner="o", repo="r", agent_tools=AgentToolsConfig(enabled=True))
        pr = PullRequestFactory(head_sha="a" * 40)
        with (
            patch.object(drafter, "_get_docker_client", return_value=None),
            drafter._tool_session(pr, cfg, Path("/repo")) as (runner, _registry, specs),
        ):
            assert runner is None
            assert specs == []

    def test_happy_path_yields_runner_and_registry(self, tmp_path: Path) -> None:
        cfg = ProjectConfig(owner="o", repo="r", agent_tools=AgentToolsConfig(enabled=True))
        pr = PullRequestFactory(head_sha="a" * 40)
        fake_runner = FakeRunner(available={"rg", "fd", "cat", "ctags"})

        @contextmanager
        def fake_ws(repo_path, head_sha):
            yield tmp_path

        @contextmanager
        def fake_session(*args, **kwargs):
            yield fake_runner

        with (
            patch.object(drafter, "_get_docker_client", return_value=MagicMock()),
            patch("franktheunicorn.worker.test_workspace.pr_branch_workspace", fake_ws),
            patch("franktheunicorn.worker.test_image.resolve_image", return_value="img"),
            patch("franktheunicorn.worker.tool_sandbox.tool_sandbox_session", fake_session),
            drafter._tool_session(pr, cfg, Path("/repo")) as (runner, registry, specs),
        ):
            assert runner is fake_runner
            assert "grep" in registry
            assert len(specs) == len(registry)


@pytest.mark.django_db
class TestRunSingleBackendTools:
    def _registry_and_specs(self):
        runner = FakeRunner(available={"rg", "cat"})
        cfg = AgentToolsConfig(enabled=True)
        registry = build_tool_registry(cfg, runner)
        return runner, registry, anthropic_tool_specs(registry), cfg

    def test_attaches_tools_for_claude(self) -> None:
        runner, registry, specs, cfg = self._registry_and_specs()
        backend = MagicMock(spec=BaseLLMBackend)
        with patch.object(drafter, "get_backend", return_value=backend):
            drafter._run_single_backend(
                LLMBackendConfig(provider="claude"),
                "diff",
                make_pr_context(),
                tool_runner=runner,
                tool_registry=registry,
                tool_specs=specs,
                tools_cfg=cfg,
            )
        backend.attach_tools.assert_called_once()

    def test_does_not_attach_for_openai(self) -> None:
        runner, registry, specs, cfg = self._registry_and_specs()
        backend = MagicMock(spec=BaseLLMBackend)
        with patch.object(drafter, "get_backend", return_value=backend):
            drafter._run_single_backend(
                LLMBackendConfig(provider="openai"),
                "diff",
                make_pr_context(),
                tool_runner=runner,
                tool_registry=registry,
                tool_specs=specs,
                tools_cfg=cfg,
            )
        backend.attach_tools.assert_not_called()

    def test_no_runner_means_no_attach(self) -> None:
        backend = MagicMock(spec=BaseLLMBackend)
        with patch.object(drafter, "get_backend", return_value=backend):
            drafter._run_single_backend(
                LLMBackendConfig(provider="claude"),
                "diff",
                make_pr_context(),
            )
        backend.attach_tools.assert_not_called()


@pytest.mark.django_db
class TestDraftReviewDefaultOff:
    def test_draft_review_does_not_touch_docker_when_disabled(
        self,
        db_pr,
        spark_project_config,
        operator_config,
    ) -> None:
        with patch.object(drafter, "_get_docker_client") as mock_docker:
            drafter.draft_review(db_pr, spark_project_config, operator_config=operator_config)
            mock_docker.assert_not_called()
