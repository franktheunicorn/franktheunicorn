"""Tests for the "CI Failures" tab, its per-PR panel, and its four endpoints."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.utils import timezone

from franktheunicorn.config.models import CIFailureConfig, ProjectConfig
from franktheunicorn.core.models import (
    CICheckHistory,
    CIFailureAssessment,
    OperatorAction,
    Project,
    PullRequest,
    WorkerCommand,
)
from franktheunicorn.security.queue import PRIORITY_BULK, PRIORITY_INTERACTIVE
from tests.factories import (
    CIFailureAssessmentFactory,
    ProjectFactory,
    PullRequestFactory,
    WorkerCommandFactory,
)

_VIEWS = "franktheunicorn.dashboard.views"
TAB = "/?queue=ci-failures"
LINT_JOB = "Run / Linters, licenses, and dependencies"

RED = [{"name": "Build", "status": "completed", "conclusion": "failure"}]
GREEN = [{"name": "Build", "status": "completed", "conclusion": "success"}]


@pytest.fixture
def project(db: Any) -> Project:
    return ProjectFactory(owner="apache", repo="spark")


def _red_pr(project: Project, **kwargs: Any) -> PullRequest:
    defaults: dict[str, Any] = {
        "project": project,
        "author": "newbie",
        "head_sha": "head1",
        "ci_checks_sha": "head1",
        "ci_status": "fail",
        "ci_checks": RED,
        "github_updated_at": timezone.now(),
    }
    defaults.update(kwargs)
    return PullRequestFactory(**defaults)


def _classified(project: Project, **kwargs: Any) -> CIFailureAssessment:
    pr = kwargs.pop("pull_request", None) or _red_pr(project)
    defaults: dict[str, Any] = {
        "pull_request": pr,
        "status": "classified",
        "cause": "pr",
        "categories": ["lint"],
        "summary": "Style / lint check",
        "comment_body": "Hi @newbie, the Scala linter failed. Run `./dev/lint-scala`.",
        "classified_at": timezone.now(),
        "failures": [
            {
                "jobs": [LINT_JOB],
                "step": "Scala linter",
                "category": "lint",
                "cause": "pr",
                "summary": "The Scala style check failed.",
                "remediation": "Run `./dev/lint-scala` locally.",
                "evidence": "[error] Foo.scala:9: line too long",
                "source": "rule",
                "rule": "spark-scala-lint",
                "url": "https://github.com/newbie/spark/actions/runs/1/job/2",
            }
        ],
    }
    defaults.update(kwargs)
    return CIFailureAssessmentFactory(**defaults)


def _forge(**kwargs: Any) -> MagicMock:
    """A posting client whose live reads say: open, same head, still red."""
    forge = MagicMock()
    forge.get_pull_request.return_value = kwargs.get(
        "detail", {"state": "open", "head": {"sha": "head1"}}
    )
    forge.get_commit_checks.return_value = kwargs.get("checks", RED)
    forge.create_issue_comment.return_value = {"id": 4321}
    return forge


def _post(client: Client, pr: PullRequest, forge: Any, **data: str) -> str:
    with (
        patch(f"{_VIEWS}._make_posting_client", return_value=forge),
        patch(f"{_VIEWS}._operator_username", return_value="holdenk"),
    ):
        response = client.post(f"/pr/{pr.pk}/ci-failure/post/", data)
    assert response.status_code == 200
    return response.content.decode()


@pytest.mark.django_db
class TestCIFailuresTab:
    def test_tab_is_in_the_bar(self, client: Client) -> None:
        assert "CI Failures" in client.get("/").content.decode()

    def test_lists_what_is_red_and_unanswered_with_its_draft(
        self, client: Client, project: Project
    ) -> None:
        row = _classified(project)
        waiting = CIFailureAssessmentFactory(pull_request=_red_pr(project, title="Waiting PR"))
        _classified(project, pull_request=_red_pr(project, title="Posted PR"), disposition="posted")
        _classified(
            project, pull_request=_red_pr(project, title="Dismissed PR"), disposition="dismissed"
        )
        _classified(project, pull_request=_red_pr(project, title="Green PR", ci_status="pass"))

        html = client.get(TAB).content.decode()
        assert f'id="ci-failure-{row.pull_request_id}"' in html
        assert f'id="ci-failure-{waiting.pull_request_id}"' in html
        for gone in ("Posted PR", "Dismissed PR", "Green PR"):
            assert gone not in html
        # The classified row: verdict, what failed, the fix, the log line, the draft.
        assert "Author's to fix" in html
        assert "Style / lint check" in html
        assert "Scala linter" in html
        assert "./dev/lint-scala" in html
        assert "Foo.scala:9" in html
        assert "Hi @newbie, the Scala linter failed." in html
        assert "Post comment" in html
        # The unclassified one says so and offers to classify.
        assert "Not yet classified" in html
        assert "Classify CI failure" in html

    def test_finding_text_is_inline_markdown_with_html_escaped(
        self, client: Client, project: Project
    ) -> None:
        """A summary can carry text a model lifted from a log an attacker wrote."""
        _classified(
            project,
            failures=[
                {
                    "jobs": ["tests"],
                    "category": "test",
                    "cause": "pr",
                    "summary": "`FooSuite` failed <script>alert(1)</script>",
                    "remediation": "Run **this** [here](https://evil.example)",
                    "source": "llm",
                }
            ],
        )
        html = client.get(TAB).content.decode()
        assert "<code>FooSuite</code> failed &lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "<strong>this</strong>" in html
        assert 'href="https://evil.example"' not in html

    def test_the_tab_count_matches_its_rows(self, client: Client, project: Project) -> None:
        _classified(project)
        CIFailureAssessmentFactory(pull_request=_red_pr(project))
        _red_pr(project)  # red, but nobody has looked: not in the queue yet
        response = client.get(TAB)
        assert response.context["queue_counts"]["ci-failures"] == 2
        assert len(response.context["pull_requests"]) == 2

    def test_classified_rows_come_first(self, client: Client, project: Project) -> None:
        waiting = CIFailureAssessmentFactory(pull_request=_red_pr(project))
        done = _classified(project)
        prs = client.get(TAB).context["pull_requests"]
        assert [pr.pk for pr in prs] == [done.pull_request_id, waiting.pull_request_id]

    def test_other_tabs_do_not_render_the_panel(self, client: Client, project: Project) -> None:
        _classified(project)
        assert "ci-failure-" not in client.get("/?queue=review").content.decode()

    def test_header_says_nothing_classifies_on_its_own_by_default(self, client: Client) -> None:
        with patch(f"{_VIEWS}._project_configs_by_name", return_value={}):
            html = client.get(TAB).content.decode()
        assert "Find failing PRs" in html
        assert "ci_failure.enabled: true" in html
        assert "No CI failures waiting for you." in html

    def test_header_names_the_projects_that_opted_in_and_what_is_in_flight(
        self, client: Client, project: Project
    ) -> None:
        pc = ProjectConfig(owner="apache", repo="spark", ci_failure=CIFailureConfig(enabled=True))
        pr = _red_pr(project)
        CIFailureAssessmentFactory(pull_request=pr)
        WorkerCommandFactory(command="classify_pr_ci", pull_request=pr)
        with patch(f"{_VIEWS}._project_configs_by_name", return_value={"apache/spark": pc}):
            html = client.get(TAB).content.decode()
        assert "Classified automatically each cycle for apache/spark." in html
        assert "1 classification queued or running" in html
        assert "Queued for the worker" in html  # the row itself says it too

    def test_own_pr_shows_the_diagnosis_but_no_post_button(
        self, client: Client, project: Project
    ) -> None:
        _classified(project, pull_request=_red_pr(project, is_operator_pr=True))
        html = client.get(TAB).content.decode()
        assert "Your own PR" in html
        assert ">Post comment<" not in html

    def test_a_draft_that_matches_an_anti_pattern_says_so(
        self, client: Client, project: Project
    ) -> None:
        _classified(project, anti_pattern_hits=["thanks for the PR"])
        assert "matches 1 of your anti-pattern" in client.get(TAB).content.decode()

    def test_an_earlier_comment_on_the_same_pr_is_flagged(
        self, client: Client, project: Project
    ) -> None:
        _classified(project, posted_at=timezone.now(), posted_sha="head0", post_count=1)
        assert "you commented on this PR's CI" in client.get(TAB).content.decode()


@pytest.mark.django_db
class TestDetailPage:
    def test_a_red_pr_nobody_classified_gets_the_offer(
        self, client: Client, project: Project
    ) -> None:
        pr = _red_pr(project)
        html = client.get(f"/pr/{pr.pk}/").content.decode()
        assert "CI Failure" in html
        assert "Classify CI failure" in html
        assert "Not classified yet." in html

    def test_a_pr_not_known_to_be_red_has_no_panel_but_can_still_be_asked_about(
        self, client: Client, project: Project
    ) -> None:
        """The stored CI state can be behind (or never read); the button re-reads it."""
        pr = _red_pr(project, ci_status="", ci_checks=[])
        html = client.get(f"/pr/{pr.pk}/").content.decode()
        assert f'id="ci-failure-{pr.pk}"' not in html
        assert 'hx-target="#classify-ci-result"' in html
        # Pressing it answers with the panel, which carries its own buttons from then on.
        panel = client.post(f"/pr/{pr.pk}/ci-failure/classify/").content.decode()
        assert f'id="ci-failure-{pr.pk}"' in panel
        assert "Queued for the worker" in panel
        # ...and the page then renders the panel instead of the stand-alone button.
        html = client.get(f"/pr/{pr.pk}/").content.decode()
        assert f'id="ci-failure-{pr.pk}"' in html
        assert 'hx-target="#classify-ci-result"' not in html

    def test_a_closed_pr_gets_no_classify_button(self, client: Client, project: Project) -> None:
        pr = _red_pr(project, ci_status="", state="closed")
        assert "classify-ci-result" not in client.get(f"/pr/{pr.pk}/").content.decode()

    def test_the_assessment_is_shown_with_who_decided(
        self, client: Client, project: Project
    ) -> None:
        row = _classified(project)
        html = client.get(f"/pr/{row.pull_request_id}/").content.decode()
        assert "decided by: rule (spark-scala-lint)" in html
        assert "rules only" in html
        assert "Re-classify" in html

    def test_a_failed_worker_command_is_surfaced(self, client: Client, project: Project) -> None:
        pr = _red_pr(project)
        WorkerCommandFactory(
            command="classify_pr_ci", pull_request=pr, status="failed", error="forge said 502"
        )
        html = client.get(f"/pr/{pr.pk}/").content.decode()
        assert "Last classification failed: forge said 502" in html


@pytest.mark.django_db
class TestScan:
    def test_finds_the_red_prs_and_queues_them_without_the_flag(
        self, client: Client, project: Project
    ) -> None:
        pr = _red_pr(project)
        pc = ProjectConfig(owner="apache", repo="spark")  # ci_failure.enabled is False
        with patch(f"{_VIEWS}._project_configs_by_name", return_value={"apache/spark": pc}):
            response = client.post("/ci-failures/scan/")
        assert response.status_code == 302
        assert response["Location"].endswith("?queue=ci-failures")
        row = CIFailureAssessment.objects.get(pull_request=pr)
        assert row.status == "unclassified"
        command = WorkerCommand.objects.get(command="classify_pr_ci")
        assert (command.pull_request_id, command.priority) == (pr.pk, PRIORITY_BULK)
        flashed = [str(m) for m in get_messages(response.wsgi_request)]
        assert any("Queued 1 CI-failure classification" in m for m in flashed)

    def test_nothing_red_says_where_the_ci_state_comes_from(
        self, client: Client, project: Project
    ) -> None:
        pc = ProjectConfig(owner="apache", repo="spark")
        with patch(f"{_VIEWS}._project_configs_by_name", return_value={"apache/spark": pc}):
            response = client.post("/ci-failures/scan/")
        flashed = [str(m) for m in get_messages(response.wsgi_request)]
        assert any("No open PR has failing CI" in m for m in flashed)
        assert not WorkerCommand.objects.exists()

    def test_a_second_press_queues_nothing_new(self, client: Client, project: Project) -> None:
        _red_pr(project)
        pc = ProjectConfig(owner="apache", repo="spark")
        with patch(f"{_VIEWS}._project_configs_by_name", return_value={"apache/spark": pc}):
            client.post("/ci-failures/scan/")
            response = client.post("/ci-failures/scan/")
        assert WorkerCommand.objects.count() == 1
        flashed = [str(m) for m in get_messages(response.wsgi_request)]
        assert any("Nothing new to classify" in m for m in flashed)

    def test_get_is_refused(self, client: Client) -> None:
        assert client.get("/ci-failures/scan/").status_code == 405


@pytest.mark.django_db
class TestClassifyButton:
    def test_queues_at_interactive_priority_and_says_so(
        self, client: Client, project: Project
    ) -> None:
        pr = _red_pr(project)
        html = client.post(f"/pr/{pr.pk}/ci-failure/classify/").content.decode()
        assert "Classification queued at the front of the worker queue" in html
        command = WorkerCommand.objects.get(command="classify_pr_ci")
        assert (command.pull_request_id, command.priority) == (pr.pk, PRIORITY_INTERACTIVE)
        assert CIFailureAssessment.objects.get(pull_request=pr).status == "unclassified"

    def test_a_double_click_is_one_run(self, client: Client, project: Project) -> None:
        pr = _red_pr(project)
        client.post(f"/pr/{pr.pk}/ci-failure/classify/")
        html = client.post(f"/pr/{pr.pk}/ci-failure/classify/").content.decode()
        assert "already queued or running" in html
        assert WorkerCommand.objects.count() == 1

    def test_a_click_promotes_the_run_the_bulk_pass_queued(
        self, client: Client, project: Project
    ) -> None:
        """Otherwise the PR somebody is looking at waits behind the whole batch."""
        pr = _red_pr(project)
        queued = WorkerCommandFactory(
            command="classify_pr_ci", pull_request=pr, priority=PRIORITY_BULK
        )
        client.post(f"/pr/{pr.pk}/ci-failure/classify/")
        queued.refresh_from_db()
        assert queued.priority == PRIORITY_INTERACTIVE
        assert WorkerCommand.objects.count() == 1

    def test_a_running_command_is_not_reprioritised(self, client: Client, project: Project) -> None:
        pr = _red_pr(project)
        running = WorkerCommandFactory(
            command="classify_pr_ci", pull_request=pr, priority=PRIORITY_BULK, status="running"
        )
        client.post(f"/pr/{pr.pk}/ci-failure/classify/")
        running.refresh_from_db()
        assert running.priority == PRIORITY_BULK

    def test_reclassifying_clears_a_dismissal_and_the_retry_cap(
        self, client: Client, project: Project
    ) -> None:
        row = _classified(
            project, disposition="dismissed", dismiss_reason="wrong", attempts=3, last_error="x"
        )
        client.post(f"/pr/{row.pull_request_id}/ci-failure/classify/")
        row.refresh_from_db()
        assert (row.status, row.disposition, row.dismiss_reason) == ("unclassified", "pending", "")
        assert (row.attempts, row.last_error) == (0, "")


@pytest.mark.django_db
class TestPost:
    def test_posts_the_draft_as_a_conversation_comment(
        self, client: Client, project: Project
    ) -> None:
        row = _classified(project)
        pr = row.pull_request
        forge = _forge()
        html = _post(client, pr, forge)
        forge.create_issue_comment.assert_called_once_with(
            "apache", "spark", pr.number, row.comment_body
        )
        forge.close.assert_called_once()
        row.refresh_from_db()
        assert (row.disposition, row.forge_comment_id, row.posted_sha) == ("posted", 4321, "head1")
        assert row.edited_body == ""
        assert "Posted on" in html
        assert "posted" in html
        assert OperatorAction.objects.filter(action_type="post_ci_failure_comment").exists()
        # Posting "this is your change" is the operator calling the failure real.
        assert CICheckHistory.objects.get(check_name=LINT_JOB).real_count == 1

    def test_the_operators_edit_is_what_goes_out_and_is_kept(
        self, client: Client, project: Project
    ) -> None:
        row = _classified(project)
        forge = _forge()
        _post(client, row.pull_request, forge, body="Hi @newbie, cc @someone-else: run the linter.")
        sent = forge.create_issue_comment.call_args.args[3]
        # The author stays a live mention; anyone else is backticked.
        assert sent == "Hi @newbie, cc `@someone-else`: run the linter."
        row.refresh_from_db()
        assert row.edited_body == sent

    def test_a_forge_without_issue_comments_gets_a_comment_review(
        self, client: Client, project: Project
    ) -> None:
        row = _classified(project)
        forge = _forge()
        forge.create_issue_comment.side_effect = NotImplementedError
        forge.create_review.return_value = {"id": 77}
        _post(client, row.pull_request, forge)
        review = forge.create_review.call_args.args[3]
        assert (review.event, review.body) == ("COMMENT", row.comment_body)
        row.refresh_from_db()
        assert row.disposition == "posted"

    @pytest.mark.parametrize(
        ("forge_kwargs", "needle"),
        [
            ({"detail": {"state": "open", "head": {"sha": "head2"}}}, "new commits"),
            ({"detail": {"state": "closed", "head": {"sha": "head1"}}}, "no longer open"),
            ({"checks": GREEN}, "no longer failing"),
        ],
    )
    def test_a_stale_draft_is_not_posted_and_the_override_is_offered(
        self, client: Client, project: Project, forge_kwargs: dict[str, Any], needle: str
    ) -> None:
        row = _classified(project)
        forge = _forge(**forge_kwargs)
        html = _post(client, row.pull_request, forge)
        forge.create_issue_comment.assert_not_called()
        forge.close.assert_called_once()
        assert "Not posted:" in html and needle in html
        assert "Post anyway" in html
        row.refresh_from_db()
        assert row.disposition == "pending"

    def test_a_refusal_hands_the_operators_edit_back(
        self, client: Client, project: Project
    ) -> None:
        """Re-rendering the stored draft would throw away what they just typed."""
        row = _classified(project)
        forge = _forge(detail={"state": "open", "head": {"sha": "head2"}})
        html = _post(client, row.pull_request, forge, body="My rewritten comment.")
        assert ">My rewritten comment.</textarea>" in html
        assert '<details class="cif-draft" open>' in html
        row.refresh_from_db()
        assert row.edited_body == ""  # nothing was posted, so nothing was recorded

    def test_an_emptied_box_is_not_replaced_by_the_draft(
        self, client: Client, project: Project
    ) -> None:
        row = _classified(project)
        forge = _forge()
        html = _post(client, row.pull_request, forge, body="   ")
        assert "the comment is empty" in html
        forge.create_issue_comment.assert_not_called()

    def test_post_anyway_skips_the_live_check(self, client: Client, project: Project) -> None:
        row = _classified(project)
        forge = _forge(checks=GREEN)
        _post(client, row.pull_request, forge, force="1")
        forge.get_pull_request.assert_not_called()
        forge.create_issue_comment.assert_called_once()

    def test_refusals_that_never_reach_the_forge(self, client: Client, project: Project) -> None:
        forge = _forge()
        unclassified = CIFailureAssessmentFactory(pull_request=_red_pr(project))
        assert "has not been classified yet" in _post(client, unclassified.pull_request, forge)

        nothing = _red_pr(project)
        assert "has not been classified yet" in _post(client, nothing, forge)

        own = _classified(project, pull_request=_red_pr(project, author="HoldenK"))
        assert "your own PR" in _post(client, own.pull_request, forge)

        done = _classified(project, disposition="posted")
        assert "Already posted" in _post(client, done.pull_request, forge)

        old = _classified(
            project, pull_request=_red_pr(project, head_sha="head2"), head_sha="head1"
        )
        assert "older commit" in _post(client, old.pull_request, forge)

        forge.create_issue_comment.assert_not_called()
        forge.get_pull_request.assert_not_called()

    def test_no_token_and_a_forge_error_are_reported_not_raised(
        self, client: Client, project: Project
    ) -> None:
        row = _classified(project)
        assert "no forge client/token" in _post(client, row.pull_request, None)

        forge = _forge()
        forge.create_issue_comment.side_effect = RuntimeError("422")
        assert "Comment failed" in _post(client, row.pull_request, forge)
        row.refresh_from_db()
        assert row.disposition == "pending"


@pytest.mark.django_db
class TestDismiss:
    def test_its_a_flake_is_remembered_against_the_job(
        self, client: Client, project: Project
    ) -> None:
        row = _classified(project)
        html = client.post(
            f"/pr/{row.pull_request_id}/ci-failure/dismiss/",
            {"reason": "flake", "note": "linter image was down"},
        ).content.decode()
        row.refresh_from_db()
        assert (row.disposition, row.dismiss_reason, row.dismiss_note) == (
            "dismissed",
            "flake",
            "linter image was down",
        )
        assert CICheckHistory.objects.get(check_name=LINT_JOB).flaky_count == 1
        assert "Noted against the failed job(s)" in html
        assert "Restore" in html
        assert row.pull_request_id not in [pr.pk for pr in client.get(TAB).context["pull_requests"]]

    def test_unknown_reason_changes_nothing(self, client: Client, project: Project) -> None:
        row = _classified(project)
        html = client.post(
            f"/pr/{row.pull_request_id}/ci-failure/dismiss/", {"reason": "because"}
        ).content.decode()
        assert "Unknown reason." in html
        row.refresh_from_db()
        assert row.disposition == "pending"

    def test_restore_puts_it_back(self, client: Client, project: Project) -> None:
        row = _classified(project, disposition="dismissed", dismiss_reason="not-worth")
        client.post(f"/pr/{row.pull_request_id}/ci-failure/dismiss/", {"restore": "1"})
        row.refresh_from_db()
        assert row.disposition == "pending"
        assert row.pull_request_id in [pr.pk for pr in client.get(TAB).context["pull_requests"]]

    def test_dismissing_a_pr_with_no_assessment_is_a_404(
        self, client: Client, project: Project
    ) -> None:
        pr = _red_pr(project)
        assert client.post(f"/pr/{pr.pk}/ci-failure/dismiss/").status_code == 404
