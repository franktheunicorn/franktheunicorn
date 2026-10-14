"""Tests for the closed-PR follow-up scan behind the Zombie Queue."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from franktheunicorn.config.models import JiraConfig, OperatorConfig, ProjectConfig
from franktheunicorn.core.models import (
    ZOMBIE_QUEUE,
    Project,
    PullRequest,
    ZombieAssessment,
    ZombieScanState,
)
from franktheunicorn.data_access.base import FetchMethod
from franktheunicorn.data_access.jira.types import JiraTicketResult
from franktheunicorn.review.zombie import (
    ZombieScanRun,
    assess_pr,
    resolve_backend,
    scan_project,
    zombie_gate_reason,
)
from tests.factories import (
    CannedLLMBackend,
    ProjectFactory,
    PullRequestFactory,
    ZombieAssessmentFactory,
    ZombieScanStateFactory,
)


def _pr_data(
    number: int, *, merged: bool = False, title: str = "", body: str = ""
) -> dict[str, Any]:
    return {
        "id": 100_000 + number,
        "number": number,
        "title": title or f"[SPARK-{number}] Fix thing {number}",
        "body": body,
        "user": {"login": f"dev{number}"},
        "state": "closed",
        "html_url": f"https://github.com/apache/spark/pull/{number}",
        "merged_at": "2026-01-02T00:00:00Z" if merged else None,
        "closed_at": "2026-01-02T00:00:00Z",
        "created_at": "2025-12-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "labels": [],
        "requested_reviewers": [],
        "assignees": [],
        "draft": False,
    }


def _ticket(ticket_id: str, status: str = "Open") -> JiraTicketResult:
    return JiraTicketResult(
        fetched_via=FetchMethod.API,
        ticket_id=ticket_id,
        summary=f"Summary of {ticket_id}",
        description="A description",
        status=status,
    )


class FakeForge:
    """A forge whose closed-PR list is a dict of sort -> pages.

    Records every page requested so a test can assert how much listing a run
    spent, which is the thing the budget and the cursor exist to bound.
    """

    def __init__(self, pages: dict[str, list[list[dict[str, Any]]]]) -> None:
        self.pages = pages
        self.requests: list[tuple[str, int]] = []
        self.diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
        self.comments: list[dict[str, Any]] = [
            {
                "user": {"login": "github-actions[bot]"},
                "created_at": "2026-01-02T00:00:00Z",
                "body": "We're closing this PR because it hasn't been updated in a while.",
            }
        ]
        self.closed = False

    def list_pull_requests_page(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "closed",
        page: int = 1,
        per_page: int = 100,
        sort: str = "created",
    ) -> list[dict[str, Any]]:
        self.requests.append((sort, page))
        pages = self.pages.get(sort, [])
        return pages[page - 1] if page <= len(pages) else []

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        """The retry pass's per-PR fetch: a closed, unmerged PR naming its ticket."""
        return _pr_data(pr_number)

    def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
        return self.diff

    def get_issue_comments(self, owner: str, repo: str, issue_number: int) -> list[dict[str, Any]]:
        return self.comments

    def close(self) -> None:
        self.closed = True


def _spark_config() -> ProjectConfig:
    return ProjectConfig(
        owner="apache",
        repo="spark",
        jira=JiraConfig(
            enabled=True, server="https://issues.apache.org/jira", project_prefix="SPARK"
        ),
    )


_ANSWER = json.dumps(
    {
        "jira_makes_sense": True,
        "jira_reasoning": "The bug is still there.",
        "fix_makes_sense": "yes",
        "fix_reasoning": "Right place, missing a test.",
        "verdict": "revive-with-changes",
        "needed_changes": ["Rebase on master", "Add a regression test"],
        "summary": "Worth reviving once rebased with a test.",
        "confidence": 0.8,
    }
)


@pytest.fixture
def spark(db: Any) -> Project:
    return ProjectFactory(owner="apache", repo="spark")


@pytest.fixture
def jira_statuses() -> dict[str, str]:
    """Ticket id -> status the fake JiraFetcher answers with."""
    return {}


@pytest.fixture(autouse=True)
def _fake_jira(monkeypatch: pytest.MonkeyPatch, jira_statuses: dict[str, str]) -> None:
    class FakeJira:
        def __init__(self, client: Any) -> None:
            pass

        def fetch(self, server: str, ticket_id: str) -> JiraTicketResult:
            status = jira_statuses.get(ticket_id, "Open")
            if status == "boom":
                raise RuntimeError("jira down")
            return _ticket(ticket_id, status)

    monkeypatch.setattr("franktheunicorn.review.zombie.JiraFetcher", FakeJira)


@pytest.fixture(autouse=True)
def _spark_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _spark_config()
    monkeypatch.setattr(
        "franktheunicorn.config.loader.get_project_config",
        lambda name: cfg if name.lower() == "apache/spark" else None,
    )


def _run(
    spark: Project, forge: FakeForge, backend: CannedLLMBackend | None = None, **kwargs: Any
) -> ZombieScanRun:
    return scan_project(
        spark,
        OperatorConfig(),
        forge_client=forge,
        backend=backend,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.django_db
class TestScanProject:
    def test_keeps_only_closed_unmerged_prs_with_an_open_jira(
        self, spark: Project, jira_statuses: dict[str, str]
    ) -> None:
        jira_statuses["SPARK-3"] = "Resolved"
        jira_statuses["SPARK-2"] = "boom"
        forge = FakeForge(
            {
                "created": [
                    [
                        _pr_data(5),  # zombie
                        _pr_data(4, merged=True),
                        _pr_data(3),  # JIRA resolved
                        _pr_data(2),  # JIRA unreachable
                        _pr_data(1, title="Fix without a ticket"),
                    ]
                ]
            }
        )
        run = _run(spark, forge, CannedLLMBackend(_ANSWER))

        assert run.examined == 5
        assert run.candidates == 1
        assert run.merged == 1
        assert run.jira_closed == 1
        assert run.jira_error == 1
        assert run.no_jira == 1
        assert run.exhausted is True
        pr = PullRequest.objects.get(project=spark, number=5)
        assert pr.state == "closed"
        assert pr.queue == ZOMBIE_QUEUE
        assert pr.jira_ticket_id == "SPARK-5"
        assert pr.jira_cache["summary"] == "Summary of SPARK-5"
        assert not PullRequest.objects.filter(project=spark, number=4).exists()
        zombie = ZombieAssessment.objects.get(pull_request=pr)
        assert zombie.jira_status == "Open"
        assert zombie.jira_url == "https://issues.apache.org/jira/browse/SPARK-5"
        assert zombie.pr_closed_at is not None
        assert "5 closed PR" in run.summary()

    def test_assessment_writes_the_models_answer(self, spark: Project) -> None:
        forge = FakeForge({"created": [[_pr_data(7)]]})
        backend = CannedLLMBackend(_ANSWER)

        run = _run(spark, forge, backend)

        assert run.assessed == 1
        zombie = ZombieAssessment.objects.get(pull_request__number=7)
        assert zombie.verdict == "revive-with-changes"
        assert zombie.jira_makes_sense is True
        assert zombie.fix_makes_sense is True
        assert zombie.needed_changes == ["Rebase on master", "Add a regression test"]
        assert zombie.summary.startswith("Worth reviving")
        assert zombie.confidence == 0.8
        assert zombie.backend_used == "stub/"
        assert zombie.assessed_at is not None
        assert zombie.attempts == 1
        assert "github-actions[bot]" in zombie.closing_context
        # The model saw the ticket, the closing comments and the diff.
        prompt = backend.calls[0]
        assert "SPARK-7" in prompt
        assert "hasn't been updated in a while" in prompt
        assert "+++ b/x" in prompt

    def test_unknown_verdict_becomes_unclear(self, spark: Project) -> None:
        forge = FakeForge({"created": [[_pr_data(8)]]})
        answer = json.dumps({"verdict": "maybe", "needed_changes": "one string", "confidence": 7})

        _run(spark, forge, CannedLLMBackend(answer))

        zombie = ZombieAssessment.objects.get(pull_request__number=8)
        assert zombie.verdict == "unclear"
        assert zombie.needed_changes == ["one string"]
        assert zombie.confidence == 1.0
        assert zombie.jira_makes_sense is None

    def test_prose_answer_counts_an_attempt_and_keeps_it_unassessed(self, spark: Project) -> None:
        forge = FakeForge({"created": [[_pr_data(9)]]})

        run = _run(spark, forge, CannedLLMBackend("I think it is fine, no JSON for you"))

        assert run.assessed == 0
        assert run.assessment_failed == 1
        zombie = ZombieAssessment.objects.get(pull_request__number=9)
        assert zombie.verdict == "unassessed"
        assert zombie.attempts == 1
        assert "without a JSON object" in zombie.last_error

    def test_backend_exception_is_recorded_not_raised(self, spark: Project) -> None:
        forge = FakeForge({"created": [[_pr_data(10)]]})
        backend = CannedLLMBackend(_ANSWER)
        backend._call_api = MagicMock(side_effect=RuntimeError("quota"))  # type: ignore[method-assign]

        run = _run(spark, forge, backend)

        assert run.assessment_failed == 1
        zombie = ZombieAssessment.objects.get(pull_request__number=10)
        assert "RuntimeError: quota" in zombie.last_error

    def test_no_backend_records_zombies_unassessed(self, spark: Project) -> None:
        forge = FakeForge({"created": [[_pr_data(11)]]})

        with patch("franktheunicorn.review.zombie.resolve_backend", return_value=None):
            run = _run(spark, forge)

        assert run.candidates == 1
        assert run.assessed == 0
        assert ZombieAssessment.objects.get(pull_request__number=11).verdict == "unassessed"

    def test_assessment_budget_is_spent_newest_first_and_resumes(self, spark: Project) -> None:
        forge = FakeForge({"created": [[_pr_data(n) for n in (30, 29, 28)]]})
        backend = CannedLLMBackend(_ANSWER)

        run = _run(spark, forge, backend, max_assessments=2)

        assert run.candidates == 3
        assert run.assessed == 2
        assessed = set(
            ZombieAssessment.objects.exclude(verdict="unassessed").values_list(
                "pull_request__number", flat=True
            )
        )
        assert assessed == {30, 29}

        # Next run: nothing new to examine (frontier), but the backlog gets done.
        run2 = _run(spark, forge, backend, max_assessments=2)
        assert run2.candidates == 0
        assert run2.assessed == 1
        assert (
            ZombieAssessment.objects.get(pull_request__number=28).verdict == "revive-with-changes"
        )

    def test_backlog_gives_up_after_max_attempts(self, spark: Project) -> None:
        ZombieAssessmentFactory(
            pull_request__project=spark,
            pull_request__number=40,
            attempts=ZombieAssessment.MAX_ATTEMPTS,
        )
        forge = FakeForge({"created": [[]]})
        backend = CannedLLMBackend(_ANSWER)

        run = _run(spark, forge, backend)

        assert run.assessed == 0
        assert backend.calls == []

    def test_already_queued_prs_are_not_duplicated(self, spark: Project) -> None:
        forge = FakeForge({"created": [[_pr_data(12)]]})
        _run(spark, forge, CannedLLMBackend(_ANSWER))

        run = _run(spark, forge, CannedLLMBackend(_ANSWER), restart=True)

        assert run.already_known == 1
        assert run.candidates == 0
        assert ZombieAssessment.objects.filter(pull_request__number=12).count() == 1

    def test_dismissed_zombies_are_not_reassessed_by_the_backlog(self, spark: Project) -> None:
        ZombieAssessmentFactory(
            pull_request__project=spark, pull_request__number=41, disposition="dismissed"
        )
        backend = CannedLLMBackend(_ANSWER)

        _run(spark, FakeForge({"created": [[]]}), backend)

        assert backend.calls == []

    def test_pr_budget_stops_mid_page_and_the_next_run_continues(self, spark: Project) -> None:
        page = [_pr_data(n) for n in range(60, 50, -1)]
        forge = FakeForge({"created": [page]})

        run = _run(spark, forge, CannedLLMBackend(_ANSWER), max_prs=4, max_assessments=0)

        assert run.examined == 4
        state = ZombieScanState.objects.get(project=spark)
        assert state.lowest_number_seen == 57
        assert state.next_page == 1
        assert state.exhausted is False

        run2 = _run(spark, forge, CannedLLMBackend(_ANSWER), max_prs=4, max_assessments=0)
        assert run2.examined == 4
        assert run2.already_known == 0
        state.refresh_from_db()
        assert state.lowest_number_seen == 53
        assert state.prs_examined == 8
        examined = set(ZombieAssessment.objects.values_list("pull_request__number", flat=True))
        assert examined == {60, 59, 58, 57, 56, 55, 54, 53}

    def test_walks_pages_and_records_the_cursor(self, spark: Project) -> None:
        page1 = [_pr_data(n, merged=True) for n in range(300, 200, -1)]
        page2 = [_pr_data(n, merged=True) for n in range(200, 100, -1)]
        page3 = [_pr_data(100)]
        forge = FakeForge({"created": [page1, page2, page3]})

        run = _run(spark, forge, CannedLLMBackend(_ANSWER), max_prs=150)

        assert run.examined == 150
        assert run.pages == 2
        state = ZombieScanState.objects.get(project=spark)
        assert state.next_page == 2
        assert state.lowest_number_seen == 151
        assert forge.requests == [("created", 1), ("created", 2)]

        run2 = _run(spark, forge, CannedLLMBackend(_ANSWER), max_prs=150)
        assert run2.examined == 51
        assert run2.exhausted is True
        assert run2.candidates == 1
        state.refresh_from_db()
        assert state.exhausted is True

    def test_exhausted_state_only_catches_up_on_recent_closures(self, spark: Project) -> None:
        forge = FakeForge({"created": [[_pr_data(1, merged=True)]]})
        _run(spark, forge, CannedLLMBackend(_ANSWER))
        state = ZombieScanState.objects.get(project=spark)
        assert state.exhausted is True
        forge.requests.clear()

        # A stale bot closed an old PR since the last run: it appears at the
        # top of the updated-sorted list, nowhere near the top of created-sorted.
        old_pr = _pr_data(1, title="[SPARK-1] old one")
        old_pr["updated_at"] = "2099-01-01T00:00:00Z"
        forge.pages["updated"] = [[old_pr, _pr_data(0, merged=True)]]
        forge.pages["updated"][0][1]["updated_at"] = "2000-01-01T00:00:00Z"

        run = _run(spark, forge, CannedLLMBackend(_ANSWER))

        assert ("updated", 1) in forge.requests
        assert ("created", 1) not in forge.requests
        assert run.examined == 1
        assert run.candidates == 1
        assert run.exhausted is True

    def test_restart_forgets_the_cursor(self, spark: Project) -> None:
        forge = FakeForge({"created": [[_pr_data(20, merged=True)]]})
        _run(spark, forge, CannedLLMBackend(_ANSWER))
        state = ZombieScanState.objects.get(project=spark)
        assert state.lowest_number_seen == 20

        run = _run(spark, forge, CannedLLMBackend(_ANSWER), restart=True)

        assert run.examined == 1
        state.refresh_from_db()
        assert state.prs_examined == 2

    def test_forge_that_cannot_page_stops_with_a_reason(self, spark: Project) -> None:
        forge = MagicMock()
        forge.list_pull_requests_page.side_effect = NotImplementedError("nope")

        run = _run(spark, forge, CannedLLMBackend(_ANSWER))

        assert "cannot page through closed PRs" in run.error
        assert run.examined == 0
        assert "stopped early" in run.summary()

    def test_listing_failure_keeps_the_old_cursor(self, spark: Project) -> None:
        forge = FakeForge({"created": [[_pr_data(n, merged=True) for n in range(100, 0, -1)]]})
        _run(spark, forge, CannedLLMBackend(_ANSWER), max_prs=100)
        state = ZombieScanState.objects.get(project=spark)
        assert state.next_page == 2

        broken = MagicMock()
        broken.list_pull_requests_page.side_effect = RuntimeError("403")
        run = _run(spark, broken, CannedLLMBackend(_ANSWER))

        assert "403" in run.error
        state.refresh_from_db()
        assert state.next_page == 2

    def test_project_without_jira_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, db: Any
    ) -> None:
        project = ProjectFactory(owner="holdenk", repo="thing")
        monkeypatch.setattr(
            "franktheunicorn.config.loader.get_project_config",
            lambda name: ProjectConfig(owner="holdenk", repo="thing"),
        )
        forge = FakeForge({"created": [[_pr_data(1)]]})

        run = scan_project(project, OperatorConfig(), forge_client=forge)  # type: ignore[arg-type]

        assert "jira.enabled" in run.skipped_reason
        assert forge.requests == []
        assert "skipped" in run.summary()

    def test_owns_the_client_it_builds(self, spark: Project) -> None:
        forge = FakeForge({"created": [[]]})
        with patch("franktheunicorn.review.zombie._forge_client", return_value=forge):
            _run(spark, FakeForge({"created": [[]]}), CannedLLMBackend(_ANSWER))
            scan_project(spark, OperatorConfig(), backend=CannedLLMBackend(_ANSWER))
        assert forge.closed is True


@pytest.mark.django_db
class TestScanCursorGaps:
    """The two ways a PR could fall between the passes, and that it doesn't."""

    def test_watermark_is_the_start_of_the_run_not_the_end(self, spark: Project) -> None:
        """A PR closed while the run was assessing has updated_at before the
        finish time; a finish-time watermark would hide it from the next catch-up."""
        forge = FakeForge({"created": [[_pr_data(1, merged=True)]]})
        before = timezone.now()
        _run(spark, forge, CannedLLMBackend(_ANSWER))
        after = timezone.now()

        state = ZombieScanState.objects.get(project=spark)
        assert state.last_run_at is not None
        assert before <= state.last_run_at <= after

        # Simulate "closed during the run": updated_at just after the watermark.
        late = _pr_data(2)
        late["updated_at"] = (state.last_run_at + timedelta(seconds=1)).isoformat()
        forge.pages["updated"] = [[late]]
        run = _run(spark, forge, CannedLLMBackend(_ANSWER))
        assert run.candidates == 1

    def test_jira_outage_is_retried_next_run(
        self, spark: Project, jira_statuses: dict[str, str]
    ) -> None:
        jira_statuses["SPARK-5"] = "boom"
        forge = FakeForge({"created": [[_pr_data(5)]]})
        run = _run(spark, forge, CannedLLMBackend(_ANSWER))
        assert run.jira_error == 1
        assert run.candidates == 0
        state = ZombieScanState.objects.get(project=spark)
        assert state.jira_retry == [5]
        assert "retried next run" in run.summary()

        # Still down: stays on the list, nothing duplicated.
        run = _run(spark, forge, CannedLLMBackend(_ANSWER))
        state.refresh_from_db()
        assert state.jira_retry == [5]
        assert run.jira_retried == 1

        # Back up: the PR is a zombie after all, and the list is cleared.
        del jira_statuses["SPARK-5"]
        run = _run(spark, forge, CannedLLMBackend(_ANSWER))
        assert run.candidates == 1
        assert ZombieAssessment.objects.filter(pull_request__number=5).exists()
        state.refresh_from_db()
        assert state.jira_retry == []

    def test_retry_skips_a_pr_that_was_reopened(self, spark: Project) -> None:
        ZombieScanStateFactory(project=spark, jira_retry=[9], exhausted=True)
        forge = FakeForge({})
        forge.get_pull_request = lambda owner, repo, number: {**_pr_data(number), "state": "open"}  # type: ignore[attr-defined]

        run = _run(spark, forge, CannedLLMBackend(_ANSWER))

        assert run.examined == 0
        assert ZombieScanState.objects.get(project=spark).jira_retry == []

    def test_retry_fetch_failure_keeps_the_number(self, spark: Project) -> None:
        ZombieScanStateFactory(project=spark, jira_retry=[9], exhausted=True)
        forge = MagicMock()
        forge.get_pull_request.side_effect = RuntimeError("502")
        forge.list_pull_requests_page.return_value = []

        _run(spark, forge, CannedLLMBackend(_ANSWER))

        assert ZombieScanState.objects.get(project=spark).jira_retry == [9]

    def test_retry_waits_when_the_budget_is_spent(self, spark: Project) -> None:
        ZombieScanStateFactory(project=spark, jira_retry=[9, 8])
        forge = FakeForge({"created": [[_pr_data(50, merged=True)]]})
        forge.get_pull_request = lambda owner, repo, number: _pr_data(number, merged=True)  # type: ignore[attr-defined]

        run = _run(spark, forge, CannedLLMBackend(_ANSWER), max_prs=1)

        assert run.examined == 1
        assert ZombieScanState.objects.get(project=spark).jira_retry == [8]

    @pytest.mark.parametrize("answer", ["{}", '{"message": "unable to assess"}'])
    def test_json_without_an_assessment_is_not_a_verdict(self, spark: Project, answer: str) -> None:
        forge = FakeForge({"created": [[_pr_data(9)]]})

        run = _run(spark, forge, CannedLLMBackend(answer))

        assert run.assessment_failed == 1
        zombie = ZombieAssessment.objects.get(pull_request__number=9)
        assert zombie.verdict == "unassessed"
        assert zombie.assessed_at is None
        assert "without an assessment" in zombie.last_error


@pytest.mark.django_db
class TestAssessPr:
    def test_creates_the_row_and_moves_the_pr_into_the_queue(self, spark: Project) -> None:
        pr = PullRequestFactory(
            project=spark, number=77, state="closed", jira_ticket_id="SPARK-77", queue="review"
        )
        backend = CannedLLMBackend(_ANSWER)
        forge = FakeForge({})

        with (
            patch("franktheunicorn.review.zombie.resolve_backend", return_value=backend),
            patch("franktheunicorn.review.zombie._forge_client", return_value=forge),
        ):
            note = assess_pr(pr, OperatorConfig())

        assert note.startswith("verdict=revive-with-changes")
        pr.refresh_from_db()
        assert pr.queue == ZOMBIE_QUEUE
        zombie = ZombieAssessment.objects.get(pull_request=pr)
        assert zombie.jira_url.endswith("/browse/SPARK-77")
        assert forge.closed is True

    def test_a_failed_rerun_raises_and_keeps_the_old_verdict(self, spark: Project) -> None:
        """Raises, so the worker command shows failed: a completed command with
        the old verdict still on the page looked like a successful refresh."""
        zombie = ZombieAssessmentFactory(
            pull_request__project=spark,
            pull_request__number=78,
            verdict="revive",
            summary="Earlier answer.",
            attempts=3,
        )
        with (
            patch(
                "franktheunicorn.review.zombie.resolve_backend",
                return_value=CannedLLMBackend("prose"),
            ),
            patch("franktheunicorn.review.zombie._forge_client", return_value=FakeForge({})),
            pytest.raises(RuntimeError, match="assessment failed"),
        ):
            assess_pr(zombie.pull_request, OperatorConfig())

        zombie.refresh_from_db()
        assert zombie.attempts == 1
        assert zombie.verdict == "revive"
        assert "without a JSON object" in zombie.last_error

    def test_no_backend_raises_with_the_setting_name(self, spark: Project) -> None:
        pr = PullRequestFactory(project=spark, number=79, state="closed")
        with (
            patch("franktheunicorn.review.zombie.resolve_backend", return_value=None),
            pytest.raises(ValueError, match=r"zombie_queue\.llm_backend"),
        ):
            assess_pr(pr, OperatorConfig())

    def test_no_jira_raises(self, monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
        project = ProjectFactory(owner="holdenk", repo="thing")
        monkeypatch.setattr(
            "franktheunicorn.config.loader.get_project_config",
            lambda name: ProjectConfig(owner="holdenk", repo="thing"),
        )
        pr = PullRequestFactory(project=project, number=1, state="closed")
        with pytest.raises(ValueError, match=r"jira\.enabled"):
            assess_pr(pr, OperatorConfig())


class TestGateAndBackend:
    def test_gate_reasons(self) -> None:
        assert "no project config" in zombie_gate_reason(None, "a/b")
        assert "jira.enabled" in zombie_gate_reason(ProjectConfig(owner="a", repo="b"), "a/b")
        assert zombie_gate_reason(_spark_config(), "apache/spark") == ""

    def test_resolve_backend_prefers_the_override(self) -> None:
        from franktheunicorn.config.models import LLMBackendConfig, ZombieQueueConfig

        config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub", model="general")],
            zombie_queue=ZombieQueueConfig(
                llm_backend=LLMBackendConfig(provider="stub", model="zombie-special")
            ),
        )
        backend = resolve_backend(config)
        assert backend is not None
        assert backend.label == "stub/zombie-special"

    def test_resolve_backend_none_when_nothing_configured(self) -> None:
        with patch("franktheunicorn.security.triage._agent_cli_triage_configs", return_value=[]):
            assert resolve_backend(OperatorConfig()) is None
