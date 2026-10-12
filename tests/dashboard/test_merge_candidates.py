"""Tests for the "Almost Ready to Merge" tab and its candidate actions."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.utils import timezone

from franktheunicorn.backends.base import ReviewBody
from franktheunicorn.config.models import MergeQueueConfig, ProjectConfig
from franktheunicorn.core.models import (
    CICheckHistory,
    MergeReadinessFeedback,
    OperatorAction,
    Project,
    PullRequest,
)
from franktheunicorn.dashboard.templatetags.score_filters import get_item
from tests.factories import (
    CICheckHistoryFactory,
    MergeReadinessFeedbackFactory,
    ProjectFactory,
    PullRequestFactory,
)

_VIEWS = "franktheunicorn.dashboard.views"


def _candidate(project: Project, **kwargs: Any) -> PullRequest:
    defaults: dict[str, Any] = {
        "project": project,
        "author": "alice",
        "head_sha": "h1",
        "mergeable": True,
        "additions": 12,
        "deletions": 3,
        "changed_files": ["sql/FooSuite.scala"],
        "review_states": {"bob": {"state": "APPROVED", "commit_id": "h1"}},
        "ci_checks": [{"name": "build", "status": "completed", "conclusion": "success"}],
        "ci_status": "pass",
        "github_updated_at": timezone.now(),
        "merge_readiness_verdict": "ready",
        "merge_readiness_score": 0.8,
        "merge_readiness_detail": {
            "reasons": ["CI green", "approved by bob"],
            "blockers": [],
            "ci": {"status": "green", "failing": [], "reasons": ["all 1 check(s) passed"]},
            "approvers": ["bob"],
            "evaluated_at": "2026-10-11T10:00:00+00:00",
        },
    }
    defaults.update(kwargs)
    return PullRequestFactory(**defaults)


def _flaky(project: Project, **kwargs: Any) -> PullRequest:
    detail = {
        "reasons": ["CI failed on slow — probably a flake"],
        "blockers": [],
        "ci": {
            "status": "flaky",
            "failing": ["slow"],
            "flake_confidence": {"slow": 0.7},
            "reasons": ["slow: timed out"],
        },
        "approvers": ["bob"],
    }
    return _candidate(
        project,
        merge_readiness_verdict="flaky-ci",
        merge_readiness_score=0.6,
        ci_checks=[
            {"name": "build", "status": "completed", "conclusion": "success", "url": ""},
            {
                "name": "slow",
                "status": "completed",
                "conclusion": "timed_out",
                "url": "https://ci/slow",
            },
        ],
        ci_status="fail",
        merge_readiness_detail=detail,
        base_branch="master",
        **kwargs,
    )


@pytest.fixture
def project(db: Any) -> Project:
    return ProjectFactory(owner="apache", repo="spark", contributors_cache=["alice"])


@pytest.fixture
def spark_config() -> ProjectConfig:
    return ProjectConfig(owner="apache", repo="spark", committers=["bob"])


@pytest.mark.django_db
class TestAlmostReadyTab:
    def test_tab_present_with_count(self, client: Client, project: Project) -> None:
        _candidate(project)
        _candidate(project, merge_readiness_verdict="flaky-ci")
        _candidate(project, merge_readiness_verdict="")
        response = client.get("/")
        html = response.content.decode()
        assert "Almost Ready to Merge" in html
        tab = next(t for t in response.context["queue_tabs"] if t["key"] == "almost-ready")
        assert tab["count"] == 2

    def test_tab_lists_only_listed_verdicts_ordered_by_readiness(
        self, client: Client, project: Project
    ) -> None:
        low = _candidate(project, merge_readiness_score=0.55, interest_score=0.9)
        high = _candidate(project, merge_readiness_score=0.9, interest_score=0.1)
        _candidate(project, merge_readiness_verdict="", interest_score=1.0)
        response = client.get("/?queue=almost-ready")
        assert response.status_code == 200
        assert [pr.pk for pr in response.context["pull_requests"]] == [high.pk, low.pk]
        html = response.content.decode()
        assert "merge-candidate" in html
        assert "approved by bob" in html
        assert "LGTM" in html

    def test_panel_shows_flaky_state_and_rebase_button(
        self, client: Client, project: Project
    ) -> None:
        _flaky(project)
        response = client.get("/?queue=almost-ready")
        html = response.content.decode()
        assert "Flaky CI" in html
        assert "70% flake" in html
        assert "Ask to rebase" in html
        assert "https://ci/slow" in html

    def test_rebase_already_requested_shows_hint(self, client: Client, project: Project) -> None:
        _flaky(project, rebase_requested_sha="h1", rebase_requested_at=timezone.now())
        html = client.get("/?queue=almost-ready").content.decode()
        assert "asked for a rebase" in html
        assert "Ask to rebase" not in html

    def test_learned_rules_header(self, client: Client, project: Project) -> None:
        _candidate(project)
        CICheckHistoryFactory(project=project, check_name="slow", flaky_count=2)
        html = client.get("/?queue=almost-ready").content.decode()
        assert "What this queue has learned" in html
        assert "slow: probably flaky" in html

    def test_own_pr_hides_lgtm(self, client: Client, project: Project) -> None:
        _candidate(project, is_operator_pr=True)
        html = client.get("/?queue=almost-ready").content.decode()
        assert 'class="action-btn approve mc-lgtm"' not in html

    def test_merge_button_when_merge_queue_enabled(self, client: Client, project: Project) -> None:
        pr = _candidate(project)
        pc = ProjectConfig(
            owner="apache",
            repo="spark",
            merge_queue=MergeQueueConfig(enabled=True, required_approvals=1),
        )
        pr.approval_count = 1
        pr.save()
        with patch(f"{_VIEWS}._project_configs_by_name", return_value={"apache/spark": pc}):
            html = client.get("/?queue=almost-ready").content.decode()
        assert "mc-merge" in html
        script = ProjectConfig(
            owner="apache",
            repo="spark",
            merge_queue=MergeQueueConfig(enabled=True, merge_script="dev/merge.py"),
        )
        with patch(f"{_VIEWS}._project_configs_by_name", return_value={"apache/spark": script}):
            html = client.get("/?queue=almost-ready").content.decode()
        assert "Copy PR #" in html

    def test_other_tabs_do_not_render_panels(self, client: Client, project: Project) -> None:
        _candidate(project, queue="review")
        html = client.get("/?queue=review").content.decode()
        assert "merge-candidate" not in html

    def test_detail_page_shows_readiness(self, client: Client, project: Project) -> None:
        pr = _candidate(
            project,
            merge_readiness_verdict="",
            merge_readiness_detail={"reasons": [], "blockers": ["CI still running"], "ci": {}},
        )
        html = client.get(f"/pr/{pr.pk}/").content.decode()
        assert "Merge Readiness" in html
        assert "CI still running" in html
        assert "Not ready" in html

    def test_get_item_filter(self) -> None:
        assert get_item({"not-ready": 3}, "not-ready") == 3
        assert get_item({"a": 1}, "b") is None
        assert get_item("nope", "a") is None

    def test_stats_section(self, client: Client, project: Project) -> None:
        pr = _candidate(project)
        MergeReadinessFeedbackFactory(
            pull_request=pr, project=project, verdict="not-ready", reason_code="needs-tests"
        )
        MergeReadinessFeedbackFactory(pull_request=pr, project=project, verdict="lgtm")
        CICheckHistoryFactory(project=project, flaky_count=1)
        html = client.get("/stats/").content.decode()
        assert "Almost Ready to Merge" in html
        assert "needs-tests" in html


@pytest.mark.django_db
class TestLGTM:
    def test_posts_approve_and_records(
        self, client: Client, project: Project, spark_config: ProjectConfig
    ) -> None:
        pr = _candidate(project)
        forge = MagicMock()
        with (
            patch(f"{_VIEWS}._make_posting_client", return_value=forge),
            patch(f"{_VIEWS}._operator_username", return_value="holdenk"),
            patch(f"{_VIEWS}._project_config_or_default", return_value=spark_config),
        ):
            response = client.post(f"/pr/{pr.pk}/lgtm/")
        assert response.status_code == 200
        args = forge.create_review.call_args.args
        assert args[:3] == ("apache", "spark", pr.number)
        assert isinstance(args[3], ReviewBody)
        assert args[3].event == "APPROVE"
        assert args[3].body == "LGTM"
        forge.close.assert_called_once()
        assert MergeReadinessFeedback.objects.get().verdict == "lgtm"
        assert OperatorAction.objects.get().action_type == "lgtm_pr"
        pr.refresh_from_db()
        assert pr.review_states["holdenk"]["state"] == "APPROVED"
        html = response.content.decode()
        assert "Approved on the forge" in html
        assert "you approved this" in html

    def test_own_pr_is_refused(self, client: Client, project: Project) -> None:
        pr = _candidate(project, author="holdenk")
        with (
            patch(f"{_VIEWS}._operator_username", return_value="holdenk"),
            patch(f"{_VIEWS}._make_posting_client") as make_client,
        ):
            response = client.post(f"/pr/{pr.pk}/lgtm/")
        make_client.assert_not_called()
        assert "LGTM your own PR" in response.content.decode()
        assert MergeReadinessFeedback.objects.count() == 0

    def test_no_client(self, client: Client, project: Project) -> None:
        pr = _candidate(project)
        with patch(f"{_VIEWS}._make_posting_client", return_value=None):
            response = client.post(f"/pr/{pr.pk}/lgtm/")
        assert "no forge client" in response.content.decode()

    def test_forge_error(self, client: Client, project: Project) -> None:
        pr = _candidate(project)
        forge = MagicMock()
        forge.create_review.side_effect = RuntimeError("422")
        with patch(f"{_VIEWS}._make_posting_client", return_value=forge):
            response = client.post(f"/pr/{pr.pk}/lgtm/")
        assert "Approve failed" in response.content.decode()
        assert MergeReadinessFeedback.objects.count() == 0

    def test_get_not_allowed(self, client: Client, project: Project) -> None:
        pr = _candidate(project)
        assert client.get(f"/pr/{pr.pk}/lgtm/").status_code == 405


@pytest.mark.django_db
class TestDismiss:
    def test_records_reason_and_leaves_the_queue(
        self, client: Client, project: Project, spark_config: ProjectConfig
    ) -> None:
        pr = _candidate(project)
        with patch(f"{_VIEWS}._project_config_or_default", return_value=spark_config):
            response = client.post(
                f"/pr/{pr.pk}/not-ready/",
                {"reason_code": "needs-tests", "reason_text": "no suite for the new path"},
            )
        assert response.status_code == 200
        fb = MergeReadinessFeedback.objects.get()
        assert fb.verdict == "not-ready"
        assert fb.reason_code == "needs-tests"
        assert fb.reason_text == "no suite for the new path"
        pr.refresh_from_db()
        assert pr.merge_readiness_dismissed_sha == "h1"
        assert pr.merge_readiness_verdict == ""
        assert "you marked this not ready at this commit" in pr.merge_readiness_detail["blockers"]
        html = response.content.decode()
        assert "Not ready (your call)" in html
        assert "Mark not ready" not in html
        # And it is gone from the tab.
        assert client.get("/?queue=almost-ready").context["pull_requests"] == []

    def test_not_a_flake_teaches_the_classifier(
        self, client: Client, project: Project, spark_config: ProjectConfig
    ) -> None:
        pr = _flaky(project)
        with patch(f"{_VIEWS}._project_config_or_default", return_value=spark_config):
            client.post(f"/pr/{pr.pk}/not-ready/", {"reason_code": "not-a-flake"})
        row = CICheckHistory.objects.get(project=project, check_name="slow")
        assert row.real_count == 1

    def test_unknown_reason_code(self, client: Client, project: Project) -> None:
        pr = _candidate(project)
        response = client.post(f"/pr/{pr.pk}/not-ready/", {"reason_code": "bogus"})
        assert "Unknown reason code" in response.content.decode()
        assert MergeReadinessFeedback.objects.count() == 0

    def test_three_dismissals_become_a_rule(
        self, client: Client, project: Project, spark_config: ProjectConfig
    ) -> None:
        with patch(f"{_VIEWS}._project_config_or_default", return_value=spark_config):
            for _ in range(3):
                pr = _candidate(project)
                client.post(f"/pr/{pr.pk}/not-ready/", {"reason_code": "needs-tests"})
        html = client.get("/?queue=almost-ready").content.decode()
        assert "no longer counts as almost ready" not in html  # empty queue: no header rules
        candidate = _candidate(project, changed_files=["sql/Foo.scala"])
        with patch(f"{_VIEWS}._project_config_or_default", return_value=spark_config):
            # Any action re-scores; refresh with a client that has nothing new.
            forge = MagicMock()
            forge.get_pull_request.return_value = {"head": {"sha": "h1"}, "mergeable": True}
            forge.list_pull_request_reviews.return_value = [
                {"user": {"login": "bob"}, "state": "APPROVED", "commit_id": "h1"}
            ]
            forge.get_commit_checks.return_value = candidate.ci_checks
            with patch(f"{_VIEWS}._make_posting_client", return_value=forge):
                client.post(f"/pr/{candidate.pk}/refresh-readiness/")
        candidate.refresh_from_db()
        assert any("no test changes" in b for b in candidate.merge_readiness_detail["blockers"])


@pytest.mark.django_db
class TestRequestRebase:
    def test_posts_comment_and_records(
        self, client: Client, project: Project, spark_config: ProjectConfig
    ) -> None:
        pr = _flaky(project)
        forge = MagicMock()
        with (
            patch(f"{_VIEWS}._make_posting_client", return_value=forge),
            patch(f"{_VIEWS}._project_config_or_default", return_value=spark_config),
        ):
            response = client.post(f"/pr/{pr.pk}/request-rebase/")
        assert response.status_code == 200
        args = forge.create_issue_comment.call_args.args
        assert args[:3] == ("apache", "spark", pr.number)
        assert args[3].startswith("Hi @alice")
        assert "`slow`" in args[3]
        assert "`master`" in args[3]
        forge.create_review.assert_not_called()
        pr.refresh_from_db()
        assert pr.rebase_requested_sha == "h1"
        assert pr.rebase_requested_checks == ["slow"]
        assert MergeReadinessFeedback.objects.get().verdict == "rebase-requested"
        html = response.content.decode()
        assert "Asked alice to rebase" in html
        assert "asked for a rebase" in html

    def test_falls_back_to_review_comment(self, client: Client, project: Project) -> None:
        pr = _flaky(project)
        forge = MagicMock()
        forge.create_issue_comment.side_effect = NotImplementedError("no")
        with patch(f"{_VIEWS}._make_posting_client", return_value=forge):
            client.post(f"/pr/{pr.pk}/request-rebase/")
        review = forge.create_review.call_args.args[3]
        assert review.event == "COMMENT"
        assert review.body.startswith("Hi @alice")

    def test_already_asked(self, client: Client, project: Project) -> None:
        pr = _flaky(project, rebase_requested_sha="h1", rebase_requested_at=timezone.now())
        with patch(f"{_VIEWS}._make_posting_client") as make_client:
            response = client.post(f"/pr/{pr.pk}/request-rebase/")
        make_client.assert_not_called()
        assert "Already asked" in response.content.decode()

    def test_no_client_and_forge_error(self, client: Client, project: Project) -> None:
        pr = _flaky(project)
        with patch(f"{_VIEWS}._make_posting_client", return_value=None):
            assert b"no forge client" in client.post(f"/pr/{pr.pk}/request-rebase/").content
        forge = MagicMock()
        forge.create_issue_comment.side_effect = RuntimeError("500")
        with patch(f"{_VIEWS}._make_posting_client", return_value=forge):
            assert b"Comment failed" in client.post(f"/pr/{pr.pk}/request-rebase/").content
        assert MergeReadinessFeedback.objects.count() == 0


@pytest.mark.django_db
class TestRefresh:
    def test_refetches_and_rescores(
        self, client: Client, project: Project, spark_config: ProjectConfig
    ) -> None:
        pr = _flaky(project)
        forge = MagicMock()
        forge.get_pull_request.return_value = {"head": {"sha": "h2"}, "mergeable": True}
        forge.list_pull_request_reviews.return_value = [
            {"user": {"login": "bob"}, "state": "APPROVED", "commit_id": "h2"}
        ]
        forge.get_commit_checks.return_value = [
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "slow", "status": "completed", "conclusion": "success"},
        ]
        with (
            patch(f"{_VIEWS}._make_posting_client", return_value=forge),
            patch(f"{_VIEWS}._project_config_or_default", return_value=spark_config),
        ):
            response = client.post(f"/pr/{pr.pk}/refresh-readiness/")
        assert response.status_code == 200
        forge.get_commit_checks.assert_called_once_with("apache", "spark", "h2")
        pr.refresh_from_db()
        assert pr.head_sha == "h2"
        assert pr.ci_status == "pass"
        assert pr.merge_readiness_verdict == "ready"
        assert "Refreshed from the forge" in response.content.decode()

    def test_rebase_outcome_is_learned_on_refresh(
        self, client: Client, project: Project, spark_config: ProjectConfig
    ) -> None:
        pr = _flaky(
            project,
            rebase_requested_sha="h1",
            rebase_requested_at=timezone.now(),
            rebase_requested_checks=["slow"],
        )
        forge = MagicMock()
        forge.get_pull_request.return_value = {"head": {"sha": "h2"}, "mergeable": True}
        forge.list_pull_request_reviews.return_value = []
        forge.get_commit_checks.return_value = [
            {"name": "slow", "status": "completed", "conclusion": "success"}
        ]
        with (
            patch(f"{_VIEWS}._make_posting_client", return_value=forge),
            patch(f"{_VIEWS}._project_config_or_default", return_value=spark_config),
        ):
            client.post(f"/pr/{pr.pk}/refresh-readiness/")
        assert CICheckHistory.objects.get(project=project, check_name="slow").flaky_count == 1
        pr.refresh_from_db()
        assert pr.rebase_requested_sha == ""

    def test_no_client_and_error(self, client: Client, project: Project) -> None:
        pr = _candidate(project)
        with patch(f"{_VIEWS}._make_posting_client", return_value=None):
            assert b"no forge client" in client.post(f"/pr/{pr.pk}/refresh-readiness/").content
        forge = MagicMock()
        forge.get_pull_request.side_effect = RuntimeError("down")
        with patch(f"{_VIEWS}._make_posting_client", return_value=forge):
            assert b"Refresh failed" in client.post(f"/pr/{pr.pk}/refresh-readiness/").content


@pytest.mark.django_db
class TestMergeRecordsFeedback:
    def test_successful_merge_is_recorded(self, client: Client, project: Project) -> None:
        from franktheunicorn.worker.merge_queue import MergeResult

        pr = _candidate(project, is_operator_pr=True, approval_count=1)
        pc = ProjectConfig(
            owner="apache",
            repo="spark",
            merge_queue=MergeQueueConfig(enabled=True, required_approvals=1),
        )
        with (
            patch("franktheunicorn.config.loader.load_project_configs", return_value=[pc]),
            patch(f"{_VIEWS}.execute_merge", create=True),
            patch("django.conf.settings.FRANK_GITHUB_TOKEN", "tok", create=True),
            patch("franktheunicorn.worker.merge_queue.execute_merge") as execute,
            patch("franktheunicorn.backends.github.GitHubClient"),
        ):
            execute.return_value = MergeResult(success=True, method="merge")
            response = client.post(f"/pr/{pr.pk}/merge/")
        assert b"Merged PR" in response.content
        assert MergeReadinessFeedback.objects.get().verdict == "merged"


@pytest.mark.django_db
class TestCandidateContextDefaults:
    def test_pr_without_config_renders_with_defaults(
        self, client: Client, project: Project
    ) -> None:
        """No YAML for the project: the panel still renders, merge is off."""
        from franktheunicorn.dashboard.views import _merge_candidate_context

        pr = _candidate(project)
        with patch("franktheunicorn.config.loader.get_project_config", return_value=None):
            ctx = _merge_candidate_context(pr, None, "holdenk")
        assert ctx["merge_enabled"] is False
        assert ctx["can_lgtm"] is True
        assert ctx["failing_checks"] == []
        assert ctx["reason_choices"] == MergeReadinessFeedback.REASON_CHOICES
