"""Tests for ``manage.py classify_ci_failures`` (the CI Failures queue, foreground)."""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from franktheunicorn.config.models import OperatorConfig, ProjectConfig
from franktheunicorn.core.models import (
    CIFailureAssessment,
    Project,
    PullRequest,
    WorkerCommand,
)
from tests.factories import ProjectFactory, PullRequestFactory

_CMD = "franktheunicorn.core.management.commands.classify_ci_failures"
SPARK = ProjectConfig(owner="apache", repo="spark")


@pytest.fixture(autouse=True)
def _configs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{_CMD}.load_project_configs", lambda _dir: [SPARK])
    monkeypatch.setattr(f"{_CMD}.get_operator_config", lambda: OperatorConfig())
    monkeypatch.setattr(f"{_CMD}.resolve_backend", lambda oc: None)


def _red_pr(number: int = 1, **kwargs: Any) -> PullRequest:
    project = Project.objects.filter(owner="apache", repo="spark").first() or ProjectFactory(
        owner="apache", repo="spark"
    )
    return PullRequestFactory(
        project=project,
        number=number,
        author="newbie",
        head_sha="head1",
        ci_checks_sha="head1",
        ci_status="fail",
        github_updated_at=timezone.now(),
        **kwargs,
    )


def _fake_classify(pr: PullRequest, operator_config: OperatorConfig) -> str:
    """Stand in for the worker's classification: one lint finding, drafted."""
    row, _ = CIFailureAssessment.objects.get_or_create(
        pull_request=pr, defaults={"head_sha": pr.head_sha}
    )
    row.status = "classified"
    row.comment_body = "Hi @newbie, run `./dev/lint-scala`."
    row.failures = [
        {
            "jobs": ["Run / Linters, licenses, and dependencies"],
            "category": "lint",
            "cause": "pr",
            "summary": "The Scala style check failed.",
            "remediation": "Run `./dev/lint-scala` locally.",
            "source": "rule",
        }
    ]
    row.save()
    return f"PR #{pr.number}: 1 failed job(s) -> 1 cause(s)"


@pytest.mark.django_db
class TestClassifyCIFailuresCommand:
    def test_classifies_in_the_foreground_and_prints_each_diagnosis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _red_pr(1)
        monkeypatch.setattr(f"{_CMD}.classify_pr", _fake_classify)
        out, err = StringIO(), StringIO()
        call_command("classify_ci_failures", "--show-comment", stdout=out, stderr=err)
        text = out.getvalue()
        assert "apache/spark: 1 PR(s) with failing CI (1 newly found" in text
        assert "PR #1: 1 failed job(s) -> 1 cause(s)" in text
        assert "[pr/rule] Style / lint check: The Scala style check failed." in text
        assert "fix: Run `./dev/lint-scala` locally." in text
        assert "Hi @newbie, run `./dev/lint-scala`." in text
        assert "No LLM backend is configured" in err.getvalue()
        assert not WorkerCommand.objects.exists()  # ran here, queued nothing

    def test_limit_bounds_the_run_and_says_what_is_left(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for number in (1, 2, 3):
            _red_pr(number)
        monkeypatch.setattr(f"{_CMD}.classify_pr", _fake_classify)
        out = StringIO()
        call_command("classify_ci_failures", "--limit", "1", stdout=out, stderr=StringIO())
        assert CIFailureAssessment.objects.filter(status="classified").count() == 1
        assert "2 more left unclassified" in out.getvalue()

    def test_queue_hands_it_to_the_worker(self) -> None:
        pr = _red_pr(1)
        out = StringIO()
        call_command("classify_ci_failures", "--queue", stdout=out)
        assert "1 queued for classification" in out.getvalue()
        assert WorkerCommand.objects.get(command="classify_pr_ci").pull_request_id == pr.pk

    def test_one_pr_by_number_ignores_the_candidate_filters(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pr = _red_pr(7, is_draft=True)  # a draft: the automatic pass would skip it
        seen: list[int] = []

        def classify(pr: PullRequest, operator_config: OperatorConfig) -> str:
            seen.append(pr.number)
            return _fake_classify(pr, operator_config)

        monkeypatch.setattr(f"{_CMD}.classify_pr", classify)
        call_command(
            "classify_ci_failures",
            "--project",
            "Apache/Spark",
            "--pr",
            "7",
            stdout=StringIO(),
            stderr=StringIO(),
        )
        assert seen == [pr.number]

    def test_errors_that_name_what_is_missing(self) -> None:
        with pytest.raises(CommandError, match="not an enabled, configured project"):
            call_command("classify_ci_failures", "--project", "nobody/nothing")
        with pytest.raises(CommandError, match="--pr needs --project"):
            call_command("classify_ci_failures", "--pr", "7", stderr=StringIO())
        ProjectFactory(owner="apache", repo="spark")
        with pytest.raises(CommandError, match="has not been ingested"):
            call_command(
                "classify_ci_failures", "--project", "apache/spark", "--pr", "7", stderr=StringIO()
            )

    def test_a_project_that_was_never_polled_says_so(self) -> None:
        out = StringIO()
        call_command("classify_ci_failures", stdout=out, stderr=StringIO())
        assert "not polled yet" in out.getvalue()
