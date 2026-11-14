"""Tests for forge-aware PR-diff fetching in ``process_pr``.

The review pipeline must fetch the diff from each project's *configured*
forge (via its ``ForgeClient``), not hard-coded public GitHub. When no client
is supplied it builds the configured forge client, and
any fetch failure degrades gracefully to the changed-files placeholder.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import ProjectConfig
from tests.factories import PullRequestFactory


def _capture_diff() -> tuple[dict[str, Any], Any]:
    """A fake ``draft_review`` that records the ``diff`` kwarg it receives."""
    captured: dict[str, Any] = {}

    def fake_draft_review(pr: Any, pc: Any, **kwargs: Any) -> list[Any]:
        captured["diff"] = kwargs.get("diff")
        return []

    return captured, fake_draft_review


@pytest.mark.django_db
class TestProcessPrForgeAwareDiff:
    def test_prefers_forge_client_diff(self) -> None:
        from franktheunicorn.worker.runner import process_pr

        pr = PullRequestFactory(state="open")
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)

        forge_client = MagicMock()
        forge_client.get_pull_request.return_value = {"state": "open", "merged": False}
        forge_client.get_pull_request_diff.return_value = "DIFF-FROM-FORGE"

        captured, fake_draft_review = _capture_diff()
        with patch("franktheunicorn.review.drafter.draft_review", side_effect=fake_draft_review):
            process_pr(pr, config, operator_config=None, force=True, forge_client=forge_client)

        # The diff came from the project's forge client, keyed on its own coords.
        forge_client.get_pull_request_diff.assert_called_once_with(
            pr.project.owner, pr.project.repo, pr.number
        )
        assert captured["diff"] == "DIFF-FROM-FORGE"

    def test_builds_and_closes_forge_client_when_none_is_supplied(self) -> None:
        from franktheunicorn.worker.runner import process_pr

        pr = PullRequestFactory(state="open")
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)
        client = MagicMock()
        client.get_pull_request.return_value = {"state": "open", "merged": False}
        client.get_pull_request_diff.return_value = "DIFF-FROM-CONFIGURED-FORGE"
        captured, fake_draft_review = _capture_diff()
        with (
            patch("franktheunicorn.review.drafter.draft_review", side_effect=fake_draft_review),
            patch("franktheunicorn.worker.commands._forge_client_for", return_value=client),
        ):
            process_pr(pr, config, operator_config=None, force=True)
        assert captured["diff"] == "DIFF-FROM-CONFIGURED-FORGE"
        client.close.assert_called_once()

    def test_degrades_to_placeholder_when_forge_diff_fails(self) -> None:
        from franktheunicorn.worker.runner import process_pr

        pr = PullRequestFactory(state="open")
        config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo)

        forge_client = MagicMock()
        forge_client.get_pull_request.return_value = {"state": "open", "merged": False}
        forge_client.get_pull_request_diff.side_effect = RuntimeError("forge down")

        captured, fake_draft_review = _capture_diff()
        with patch("franktheunicorn.review.drafter.draft_review", side_effect=fake_draft_review):
            process_pr(pr, config, operator_config=None, force=True, forge_client=forge_client)

        # Fetch raised → empty diff; draft_review still runs on the placeholder.
        assert captured["diff"] == ""


@pytest.mark.django_db
def test_llm_checks_skip_a_backend_preflight_disabled() -> None:
    """Checks use llm_backends[0]; a disabled one must not be it."""
    from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
    from franktheunicorn.worker.runner import process_pr

    pr = PullRequestFactory(state="open")
    config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo, llm_checks=["security"])
    op = OperatorConfig(
        llm_backends=[
            LLMBackendConfig(provider="openai", model="dead"),
            LLMBackendConfig(provider="openai", model="alive"),
        ],
        agent_cli_reviewers=[],
    )
    forge_client = MagicMock()
    forge_client.get_pull_request.return_value = {"state": "open", "merged": False}
    forge_client.get_pull_request_diff.return_value = "diff"

    with (
        patch("franktheunicorn.review.drafter.draft_review", return_value=[]),
        patch("franktheunicorn.review.checks.run_enabled_checks", return_value=[]) as checks,
    ):
        process_pr(
            pr,
            config,
            operator_config=op,
            force=True,
            forge_client=forge_client,
            disabled_backends=frozenset({0}),
        )

    used = checks.call_args.kwargs["operator_config"].llm_backends
    assert [b.model for b in used] == ["alive"]


@pytest.mark.django_db
def test_llm_checks_skip_when_every_backend_is_disabled() -> None:
    """An empty effective list would fall back to the stub's canned findings."""
    from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
    from franktheunicorn.worker.runner import process_pr

    pr = PullRequestFactory(state="open")
    config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo, llm_checks=["security"])
    op = OperatorConfig(
        llm_backends=[LLMBackendConfig(provider="openai", model="dead")], agent_cli_reviewers=[]
    )
    forge_client = MagicMock()
    forge_client.get_pull_request.return_value = {"state": "open", "merged": False}
    forge_client.get_pull_request_diff.return_value = "diff"

    with (
        patch("franktheunicorn.review.drafter.draft_review", return_value=[]),
        patch("franktheunicorn.review.checks.run_enabled_checks", return_value=[]) as checks,
    ):
        process_pr(
            pr,
            config,
            operator_config=op,
            force=True,
            forge_client=forge_client,
            disabled_backends=frozenset({0}),
        )

    checks.assert_not_called()


@pytest.mark.django_db
def test_llm_checks_use_healthy_tier_when_default_is_disabled() -> None:
    """A dead default must not suppress a healthy task-specific model."""
    from franktheunicorn.config.models import OperatorConfig
    from franktheunicorn.worker.runner import process_pr

    pr = PullRequestFactory(state="open")
    config = ProjectConfig(owner=pr.project.owner, repo=pr.project.repo, llm_checks=["security"])
    op = OperatorConfig.model_validate(
        {
            "llm_backends": [{"provider": "openai", "model": "dead"}],
            "agent_cli_reviewers": [],
            "model_routing": {
                "tiers": {"reasoning": {"model": "alive"}},
                "tasks": {"check:security": "reasoning"},
            },
        }
    )
    client = MagicMock()
    client.get_pull_request.return_value = {"state": "open", "merged": False}
    client.get_pull_request_diff.return_value = "diff"
    with (
        patch("franktheunicorn.review.drafter.draft_review", return_value=[]),
        patch("franktheunicorn.review.checks.run_enabled_checks", return_value=[]) as checks,
    ):
        process_pr(
            pr, config, op, force=True, forge_client=client, disabled_backends=frozenset({0})
        )
    effective = checks.call_args.kwargs["operator_config"]
    assert effective.llm_backends == []
    assert effective.model_routing.tiers["reasoning"].model == "alive"
