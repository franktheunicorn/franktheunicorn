"""Shared test fixtures for franktheunicorn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.factories import ProjectFactory, PullRequestFactory

from franktheunicorn.config.models import OperatorConfig, ProjectConfig
from franktheunicorn.core.models import Project, PullRequest, ReviewDraft


@pytest.fixture
def operator_config() -> OperatorConfig:
    return OperatorConfig(
        github_username="holdenk",
        review_style="direct but kind",
    )


@pytest.fixture
def spark_project_config() -> ProjectConfig:
    return ProjectConfig(
        owner="apache",
        repo="spark",
        review_context="ASF governance",
        watched_paths=["sql/catalyst/", "python/pyspark/"],
        frequent_contributors=["cloud-fan", "dongjoon-hyun"],
        tone="constructive",
        test_expectations="tests required",
    )


@pytest.fixture
def personal_project_config() -> ProjectConfig:
    return ProjectConfig(
        owner="holdenk",
        repo="my-django-app",
        review_context="personal project",
        watched_paths=["app/"],
        frequent_contributors=[],
        tone="friendly",
    )


@pytest.fixture
def db_project(db: Any) -> Project:
    return ProjectFactory(owner="apache", repo="spark", review_context="ASF governance")


@pytest.fixture
def sample_pr_data() -> dict[str, Any]:
    """Raw PR data as returned by the GitHub API (or mock)."""
    return {
        "id": 1001,
        "number": 42,
        "title": "Fix flaky test in scheduler module",
        "user": {"login": "alice-dev"},
        "state": "open",
        "html_url": "https://github.com/apache/spark/pull/42",
        "diff_url": "https://github.com/apache/spark/pull/42.diff",
        "body": "This PR fixes a race condition in the scheduler tests.",
        "labels": [{"name": "bug"}, {"name": "tests"}],
        "requested_reviewers": [{"login": "holdenk"}],
        "draft": False,
        "created_at": "2026-03-20T10:00:00Z",
        "updated_at": "2026-03-27T14:30:00Z",
        "additions": 15,
        "deletions": 3,
    }


@pytest.fixture
def db_pr(db: Any, db_project: Project) -> PullRequest:
    return PullRequestFactory(
        project=db_project,
        github_id=1001,
        number=42,
        title="Fix flaky test in scheduler",
        author="alice-dev",
        state="open",
        url="https://github.com/apache/spark/pull/42",
        body="Fixes flaky test.",
        labels=["bug", "tests"],
        requested_reviewers=["holdenk"],
        changed_files=["sql/catalyst/rules/Optimizer.scala", "README.md"],
        additions=15,
        deletions=3,
    )


@pytest.fixture
def review_draft(db_pr: PullRequest) -> ReviewDraft:
    return ReviewDraft.objects.create(
        pull_request=db_pr,
        file_path="test.py",
        line_number=5,
        comment_body="Consider adding a test.",
        confidence=0.7,
    )


@pytest.fixture
def make_pr(db_project: Project):
    """Factory fixture for creating test PRs with sensible defaults."""
    _counter = iter(range(2000, 3000))

    def _make(number: int | None = None, author: str = "someone", **kwargs: Any) -> PullRequest:
        if number is None:
            number = next(_counter)
        defaults: dict[str, Any] = {
            "github_id": number,
            "title": f"Test PR #{number}",
            "url": f"https://example.com/pr/{number}",
            "changed_files": ["README.md"],
        }
        defaults.update(kwargs)
        return PullRequest.objects.create(
            project=db_project,
            number=number,
            author=author,
            **defaults,
        )

    return _make


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory with sample YAML files."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    operator_yaml = tmp_path / "operator.yaml"
    operator_yaml.write_text(
        "github_username: testuser\nreview_style: direct\npoll_interval_seconds: 60\n"
    )

    project_yaml = projects_dir / "test-project.yaml"
    project_yaml.write_text(
        "owner: testorg\n"
        "repo: testrepo\n"
        "review_context: testing\n"
        "watched_paths:\n"
        "  - src/\n"
        "  - tests/\n"
        "frequent_contributors:\n"
        "  - alice\n"
        "enabled: true\n"
    )

    return tmp_path
