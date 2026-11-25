"""Approver names on the dashboard: bold for org members, bold + * for committers."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from franktheunicorn.config.models import MergeQueueConfig, ProjectConfig
from franktheunicorn.core.models import Project, PullRequest
from tests.factories import ProjectFactory, PullRequestFactory

_VIEWS = "franktheunicorn.dashboard.views"

COMMITTER = '<strong class="reviewer reviewer-committer"'
MEMBER = '<strong class="reviewer reviewer-member"'

STATES = {
    "holdenk": {"state": "APPROVED", "commit_id": "h1", "author_association": "MEMBER"},
    "asf-member": {"state": "APPROVED", "commit_id": "h1", "author_association": "MEMBER"},
    "drive-by": {"state": "APPROVED", "commit_id": "h1", "author_association": "CONTRIBUTOR"},
    "early": {"state": "APPROVED", "commit_id": "old", "author_association": "MEMBER"},
}


@pytest.fixture
def project(db: Any) -> Project:
    return ProjectFactory(owner="apache", repo="spark")


@pytest.fixture
def config() -> ProjectConfig:
    return ProjectConfig(
        owner="apache",
        repo="spark",
        committers=["HoldenK"],
        merge_queue=MergeQueueConfig(enabled=True),
    )


def _approved_pr(project: Project, **kwargs: Any) -> PullRequest:
    defaults: dict[str, Any] = {
        "project": project,
        "author": "alice",
        "head_sha": "h1",
        "review_states": STATES,
        "merge_intent": "approved",
        "merge_intent_by": "holdenk",
        "merge_intent_by_association": "MEMBER",
        "merge_intent_at": timezone.now(),
        "merge_intent_source": "review",
        "queue": "intending-to-merge",
        "merge_readiness_verdict": "ready",
        "merge_readiness_score": 0.8,
        "merge_readiness_detail": {
            "approvers": ["asf-member", "drive-by", "holdenk"],
            "stale_approvers": ["early"],
            "ci": {"status": "green"},
        },
    }
    defaults.update(kwargs)
    return PullRequestFactory(**defaults)


def _span(html: str, start_marker: str) -> str:
    start = html.index(start_marker)
    return html[start : html.index("</span>\n", start)]


@pytest.mark.django_db
class TestMergeCandidatePanel:
    def test_marks_each_approver(
        self, client: Client, project: Project, config: ProjectConfig
    ) -> None:
        _approved_pr(project)
        with patch(f"{_VIEWS}._project_configs_by_name", return_value={"apache/spark": config}):
            html = client.get("/?queue=almost-ready").content.decode()
        current = _span(html, '<span class="mc-approvals" title="Current approvals')
        assert f"{COMMITTER} " in current and "holdenk</strong>*" in current
        assert MEMBER in current and "asf-member</strong>" in current
        assert '<span class="reviewer">drive-by</span>' in current
        stale = _span(html, '<span class="mc-approvals muted"')
        assert "early</strong>" in stale and "early</strong>*" not in stale

    def test_no_config_still_bolds_members(self, client: Client, project: Project) -> None:
        _approved_pr(project)
        with patch(f"{_VIEWS}._project_configs_by_name", return_value={}):
            html = client.get("/?queue=almost-ready").content.decode()
        assert "holdenk</strong>" in html
        assert "</strong>*" not in html


@pytest.mark.django_db
class TestMergeIntentApprover:
    def test_list_row(self, client: Client, project: Project, config: ProjectConfig) -> None:
        _approved_pr(project)
        with patch(f"{_VIEWS}._project_configs_by_name", return_value={"apache/spark": config}):
            html = client.get("/?queue=intending-to-merge").content.decode()
        assert "by " in html and "holdenk</strong>*" in html

    def test_list_row_member(self, client: Client, project: Project) -> None:
        _approved_pr(project, merge_intent_by="asf-member")
        with patch(f"{_VIEWS}._project_configs_by_name", return_value={}):
            html = client.get("/?queue=intending-to-merge").content.decode()
        assert f"{MEMBER}" in html and "asf-member</strong>" in html
        assert "asf-member</strong>*" not in html

    def test_detail_page(self, client: Client, project: Project, config: ProjectConfig) -> None:
        pr = _approved_pr(project)
        with patch("franktheunicorn.config.loader.get_project_config", return_value=config):
            html = client.get(f"/pr/{pr.pk}/").content.decode()
        assert "holdenk</strong>*" in html

    def test_detail_page_plain_for_non_member(self, client: Client, project: Project) -> None:
        pr = _approved_pr(project, merge_intent_by="drive-by", merge_intent_by_association="")
        with patch("franktheunicorn.config.loader.get_project_config", return_value=None):
            html = client.get(f"/pr/{pr.pk}/").content.decode()
        assert 'by <span class="reviewer">drive-by</span>' in html

    def test_merge_queue_page(
        self, client: Client, project: Project, config: ProjectConfig
    ) -> None:
        _approved_pr(project)
        with patch("franktheunicorn.config.loader.load_project_configs", return_value=[config]):
            html = client.get("/merge-queue/").content.decode()
        assert "holdenk</strong>*" in html


@pytest.mark.django_db
def test_committer_read_at_boot_gets_the_star(client: Client, project: Project) -> None:
    project.committers_cache = ["asf-member"]
    project.save()
    _approved_pr(project, merge_intent_by="asf-member")
    with patch(f"{_VIEWS}._project_configs_by_name", return_value={}):
        html = client.get("/?queue=intending-to-merge").content.decode()
    assert "asf-member</strong>*" in html
