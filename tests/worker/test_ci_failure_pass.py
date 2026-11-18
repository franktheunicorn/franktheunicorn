"""The per-cycle CI-failure pass: find the red PRs, queue their classification."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest
from django.utils import timezone

from franktheunicorn.config.models import CIFailureConfig, ProjectConfig
from franktheunicorn.core.models import CIFailureAssessment, WorkerCommand
from franktheunicorn.worker.runner import _run_ci_failure_pass
from tests.factories import ProjectFactory, PullRequestFactory


def _red_pr(**kwargs: Any) -> Any:
    project = ProjectFactory(owner="apache", repo="spark")
    return PullRequestFactory(
        project=project,
        author="newbie",
        head_sha="head1",
        ci_checks_sha="head1",
        ci_status="fail",
        ci_checks=[{"name": "Build", "status": "completed", "conclusion": "failure"}],
        github_updated_at=timezone.now(),
        **kwargs,
    )


@pytest.mark.django_db
class TestCIFailurePass:
    def test_an_opted_in_project_gets_its_red_prs_queued_at_bulk_priority(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        pr = _red_pr()
        pc = ProjectConfig(owner="apache", repo="spark", ci_failure=CIFailureConfig(enabled=True))
        with caplog.at_level(logging.INFO, logger="franktheunicorn.review.ci_failure"):
            _run_ci_failure_pass([pc])
        assert CIFailureAssessment.objects.get(pull_request=pr).status == "unclassified"
        command = WorkerCommand.objects.get(command="classify_pr_ci")
        assert (command.pull_request_id, command.priority, command.status) == (pr.pk, 0, "pending")
        # One line a cycle saying what it found, at a level someone will see.
        assert "1 PR(s) with failing CI" in caplog.text
        assert "1 queued for classification" in caplog.text

    def test_a_project_that_did_not_opt_in_is_left_alone(self) -> None:
        _red_pr()
        _run_ci_failure_pass([ProjectConfig(owner="apache", repo="spark")])
        assert not CIFailureAssessment.objects.exists()
        assert not WorkerCommand.objects.exists()

    def test_the_budget_is_per_run(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        for number in range(1, 5):
            PullRequestFactory(
                project=project,
                number=number,
                head_sha="h",
                ci_checks_sha="h",
                ci_status="fail",
                github_updated_at=timezone.now(),
            )
        pc = ProjectConfig(
            owner="apache",
            repo="spark",
            ci_failure=CIFailureConfig(enabled=True, max_per_run=3),
        )
        _run_ci_failure_pass([pc])
        assert CIFailureAssessment.objects.count() == 4  # all found and listed
        assert WorkerCommand.objects.count() == 3  # three classified this run

    def test_a_failure_in_the_pass_does_not_take_the_cycle_down(self) -> None:
        with patch("franktheunicorn.review.ci_failure.run_pass", side_effect=RuntimeError("db")):
            _run_ci_failure_pass([ProjectConfig(owner="apache", repo="spark")])
