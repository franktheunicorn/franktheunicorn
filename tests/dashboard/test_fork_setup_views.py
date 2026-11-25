"""Tests for the "Fork Setup" tab, its per-PR panel, and its five endpoints."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.messages import get_messages
from django.test import Client

from franktheunicorn.core.models import (
    ForkSetupAssessment,
    OperatorAction,
    Project,
    PullRequest,
    WorkerCommand,
)
from franktheunicorn.security.queue import (
    PRIORITY_BULK,
    PRIORITY_INTERACTIVE,
    queue_fork_setup_diagnosis,
    queue_fork_setup_scan,
)
from tests.factories import ForkSetupAssessmentFactory, ProjectFactory, PullRequestFactory

_VIEWS = "franktheunicorn.dashboard.views"
TAB = "/?queue=fork-setup"
SETUP = [{"name": "Build", "status": "completed", "conclusion": "action_required"}]
GREEN = [{"name": "Build", "status": "completed", "conclusion": "success"}]


@pytest.fixture
def project(db: Any) -> Project:
    return ProjectFactory(owner="apache", repo="spark")


def _row(project: Project, **kwargs: Any) -> ForkSetupAssessment:
    pr_kwargs = {
        "author": "newbie",
        "head_sha": "head1",
        "ci_checks_sha": "head1",
        "ci_status": "fail",
        "ci_checks": SETUP,
        **kwargs.pop("pr", {}),
    }
    pr = PullRequestFactory(project=project, **pr_kwargs)
    return ForkSetupAssessmentFactory(pull_request=pr, **kwargs)


def _approval_row(project: Project, **kwargs: Any) -> ForkSetupAssessment:
    return _row(
        project,
        needs_author=False,
        needs_maintainer=True,
        kinds=["awaiting-approval"],
        comment_body="",
        problems=[
            {
                "kind": "awaiting-approval",
                "who": "maintainer",
                "summary": "1 workflow run(s) wait for a maintainer.",
                "url": "https://github.com/apache/spark/actions/runs/5",
            }
        ],
        awaiting_runs=[
            {
                "id": 5,
                "name": "Sneaky",
                "path": ".github/workflows/sneaky.yml",
                "url": "https://github.com/apache/spark/actions/runs/5",
                "on_base": False,
            }
        ],
        **kwargs,
    )


def _forge(**kwargs: Any) -> MagicMock:
    forge = MagicMock()
    forge.get_pull_request.return_value = kwargs.get(
        "detail", {"state": "open", "head": {"sha": "head1"}}
    )
    forge.get_commit_checks.return_value = kwargs.get("checks", SETUP)
    forge.list_runs_awaiting_approval.return_value = kwargs.get(
        "awaiting", [{"id": 5, "name": "Sneaky", "head_sha": "head1"}]
    )
    forge.create_issue_comment.return_value = {"id": 4321}
    return forge


def _post(client: Client, url: str, forge: Any, **data: str) -> str:
    with (
        patch(f"{_VIEWS}._make_posting_client", return_value=forge),
        patch(f"{_VIEWS}._operator_username", return_value="holdenk"),
    ):
        response = client.post(url, data)
    assert response.status_code == 200
    return response.content.decode()


@pytest.mark.django_db
class TestForkSetupTab:
    def test_tab_is_in_the_bar(self, client: Client) -> None:
        assert "Fork Setup" in client.get("/").content.decode()

    def test_lists_diagnosed_rows_with_their_draft(self, client: Client, project: Project) -> None:
        row = _row(project)
        approval = _approval_row(project, pr={"changed_files": [".github/workflows/sneaky.yml"]})
        _row(project, disposition="posted", pr={"title": "Posted PR"})
        _row(project, status="resolved", pr={"title": "Resolved PR"})

        html = client.get(TAB).content.decode()
        assert f'id="fork-setup-{row.pull_request_id}"' in html
        assert f'id="fork-setup-{approval.pull_request_id}"' in html
        assert "Posted PR" not in html and "Resolved PR" not in html
        assert "Author&#x27;s fork setup" in html or "Author's fork setup" in html
        assert "GitHub Actions not enabled on the fork" in html
        assert "Hi @newbie, please enable GitHub Actions on your fork." in html
        assert "Post comment" in html
        # The approval row: its button, the run, and both warnings.
        assert "Needs your approval" in html
        assert "Approve workflows" in html
        assert ".github/workflows/sneaky.yml" in html
        assert "not on" in html
        assert "This PR changes workflow files" in html
        assert "Find PRs that need setup" in html

    def test_approvals_sort_first(self, client: Client, project: Project) -> None:
        _row(project, pr={"title": "Author PR"})
        _approval_row(project, pr={"title": "Approval PR"})
        html = client.get(TAB).content.decode()
        assert html.index("Approval PR") < html.index("Author PR")

    def test_empty_state(self, client: Client, db: Any) -> None:
        assert "No fork-setup problems waiting for you." in client.get(TAB).content.decode()

    def test_detail_page_shows_the_panel(self, client: Client, project: Project) -> None:
        row = _row(project)
        html = client.get(f"/pr/{row.pull_request_id}/").content.decode()
        assert f'id="fork-setup-{row.pull_request_id}"' in html
        assert "Re-check" in html

    def test_detail_page_offers_a_check_for_an_unseen_setup_pr(
        self, client: Client, project: Project
    ) -> None:
        pr = PullRequestFactory(
            project=project, head_sha="h", ci_checks_sha="h", ci_status="fail", ci_checks=SETUP
        )
        html = client.get(f"/pr/{pr.pk}/").content.decode()
        assert "Check fork setup" in html
        green = PullRequestFactory(project=project, ci_status="pass", ci_checks=GREEN)
        assert f'id="fork-setup-{green.pk}"' not in client.get(f"/pr/{green.pk}/").content.decode()


@pytest.mark.django_db
class TestEndpoints:
    def test_scan_queues_the_sweep_once(self, client: Client, db: Any) -> None:
        response = client.post("/fork-setup/scan/")
        assert response.status_code == 302
        cmd = WorkerCommand.objects.get(command="scan_fork_setup")
        assert cmd.kwargs == {"every_project": True}
        assert cmd.priority == PRIORITY_INTERACTIVE
        response = client.post("/fork-setup/scan/", follow=True)
        assert "already queued" in " ".join(str(m) for m in get_messages(response.wsgi_request))
        assert WorkerCommand.objects.filter(command="scan_fork_setup").count() == 1

    def test_check_queues_and_resets(self, client: Client, project: Project) -> None:
        row = _row(project, disposition="dismissed", attempts=3)
        html = client.post(f"/pr/{row.pull_request_id}/fork-setup/check/").content.decode()
        assert "Check queued" in html
        row.refresh_from_db()
        assert (row.status, row.disposition, row.attempts) == ("unchecked", "pending", 0)
        assert WorkerCommand.objects.filter(
            command="diagnose_fork_setup", pull_request_id=row.pull_request_id
        ).exists()
        html = client.post(f"/pr/{row.pull_request_id}/fork-setup/check/").content.decode()
        assert "already queued" in html

    def test_check_creates_a_row(self, client: Client, project: Project) -> None:
        pr = PullRequestFactory(project=project, head_sha="h")
        client.post(f"/pr/{pr.pk}/fork-setup/check/")
        assert ForkSetupAssessment.objects.get(pull_request=pr).head_sha == "h"

    def test_post_after_a_live_check(self, client: Client, project: Project) -> None:
        row = _row(project)
        forge = _forge()
        html = _post(
            client,
            f"/pr/{row.pull_request_id}/fork-setup/post/",
            forge,
            body="Hi @newbie, enable Actions. cc @someone",
        )
        assert "Posted on" in html
        forge.create_issue_comment.assert_called_once()
        posted = forge.create_issue_comment.call_args.args[3]
        assert "@newbie" in posted and "`@someone`" in posted
        row.refresh_from_db()
        assert row.disposition == "posted" and row.forge_comment_id == 4321
        assert OperatorAction.objects.filter(action_type="post_fork_setup_comment").exists()

    def test_post_refused_when_the_build_got_linked(self, client: Client, project: Project) -> None:
        row = _row(project)
        html = _post(client, f"/pr/{row.pull_request_id}/fork-setup/post/", _forge(checks=GREEN))
        assert "Not posted: no check on this commit needs setup" in html
        # Nothing left to say, so nothing to force.
        assert "Post anyway" not in html
        row.refresh_from_db()
        assert row.status == "resolved"

    def test_post_refused_on_a_push_keeps_the_edit_then_forced(
        self, client: Client, project: Project
    ) -> None:
        row = _row(project)
        url = f"/pr/{row.pull_request_id}/fork-setup/post/"
        moved = _forge(detail={"state": "open", "head": {"sha": "head2"}})
        html = _post(client, url, moved, body="my edited text")
        assert "Not posted: the PR has new commits" in html
        assert "Post anyway" in html
        assert "my edited text" in html
        forge = _forge(detail={"state": "open", "head": {"sha": "head2"}})
        _post(client, url, forge, body="my edited text", force="1")
        forge.get_pull_request.assert_not_called()
        assert forge.create_issue_comment.call_args.args[3] == "my edited text"

    @pytest.mark.parametrize(
        ("setup", "message"),
        [
            ({"needs_author": False}, "Nothing to post"),
            ({"disposition": "posted"}, "Already posted"),
            ({"head_sha": "older"}, "older commit"),
            ({"pr": {"author": "holdenk"}}, "your own PR"),
        ],
    )
    def test_post_refusals(
        self, client: Client, project: Project, setup: dict[str, Any], message: str
    ) -> None:
        row = _row(project, **setup)
        forge = _forge()
        html = _post(client, f"/pr/{row.pull_request_id}/fork-setup/post/", forge)
        assert message in html
        forge.create_issue_comment.assert_not_called()

    def test_post_with_no_client_or_empty_body(self, client: Client, project: Project) -> None:
        row = _row(project)
        url = f"/pr/{row.pull_request_id}/fork-setup/post/"
        assert "Nothing to post: the comment is empty" in _post(client, url, _forge(), body=" ")
        assert "no forge client" in _post(client, url, None)
        broken = _forge()
        broken.create_issue_comment.side_effect = RuntimeError
        assert "Comment failed" in _post(client, url, broken)

    def test_post_falls_back_to_a_review(self, client: Client, project: Project) -> None:
        row = _row(project)
        forge = _forge()
        forge.create_issue_comment.side_effect = NotImplementedError
        forge.create_review.return_value = {"id": 7}
        _post(client, f"/pr/{row.pull_request_id}/fork-setup/post/", forge)
        forge.create_review.assert_called_once()

    def test_approve(self, client: Client, project: Project) -> None:
        row = _approval_row(project)
        forge = _forge()
        html = _post(client, f"/pr/{row.pull_request_id}/fork-setup/approve/", forge)
        assert "Approved 1 workflow run(s)" in html
        forge.approve_workflow_run.assert_called_once_with("apache", "spark", 5)
        row.refresh_from_db()
        assert not row.needs_maintainer
        forge.close.assert_called_once()

    def test_approve_refusals(self, client: Client, project: Project) -> None:
        row = _approval_row(project, head_sha="older")
        url = f"/pr/{row.pull_request_id}/fork-setup/approve/"
        assert "older commit" in _post(client, url, _forge())
        row.head_sha = "head1"
        row.save()
        assert "no forge client" in _post(client, url, None)
        unsupported = _forge()
        unsupported.list_runs_awaiting_approval.side_effect = NotImplementedError
        assert "could not list the waiting runs" in _post(client, url, unsupported)
        broken = _forge()
        broken.get_pull_request.return_value = {"state": "open", "head": {"sha": "head1"}}
        with patch("franktheunicorn.review.fork_setup.approve", side_effect=NotImplementedError):
            assert "no workflow-run approval" in _post(client, url, broken)
        with patch("franktheunicorn.review.fork_setup.approve", side_effect=RuntimeError):
            assert "Approval failed" in _post(client, url, broken)

    def test_dismiss_and_restore(self, client: Client, project: Project) -> None:
        row = _row(project)
        url = f"/pr/{row.pull_request_id}/fork-setup/dismiss/"
        html = client.post(url, {"reason": "fixed", "note": "done"}).content.decode()
        assert "Dismissed for this commit" in html
        row.refresh_from_db()
        assert (row.disposition, row.dismiss_reason, row.dismiss_note) == (
            "dismissed",
            "fixed",
            "done",
        )
        assert "Restore" in html
        assert "Unknown reason" in client.post(url, {"reason": "nope"}).content.decode()
        html = client.post(url, {"restore": "1"}).content.decode()
        assert "Back in the Fork Setup queue" in html
        row.refresh_from_db()
        assert row.disposition == "pending"


@pytest.mark.django_db
class TestQueueing:
    def test_diagnosis_promotes_a_bulk_row(self, project: Project) -> None:
        pr: PullRequest = PullRequestFactory(project=project)
        assert queue_fork_setup_diagnosis(pr, priority=PRIORITY_BULK)
        assert not queue_fork_setup_diagnosis(pr)
        assert WorkerCommand.objects.get(command="diagnose_fork_setup").priority == (
            PRIORITY_INTERACTIVE
        )

    def test_an_automatic_sweep_does_not_cover_a_press(self, db: Any) -> None:
        assert queue_fork_setup_scan()
        assert not queue_fork_setup_scan()
        assert queue_fork_setup_scan(every_project=True)
        assert not queue_fork_setup_scan(every_project=True)
        assert not queue_fork_setup_scan()
        assert WorkerCommand.objects.filter(command="scan_fork_setup").count() == 2
