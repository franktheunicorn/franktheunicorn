"""Tests for the "Almost Ready to Merge" scorer, CI flake classifier, and learning."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from django.utils import timezone

from franktheunicorn.config.models import (
    DEFAULT_REBASE_REQUEST_TEMPLATE,
    MergeReadinessConfig,
    ProjectConfig,
)
from franktheunicorn.core.models import (
    CICheckHistory,
    MergeReadinessFeedback,
    OperatorAction,
    PullRequest,
)
from franktheunicorn.scoring import merge_readiness as mr
from franktheunicorn.scoring.merge_readiness import (
    CI_FLAKY,
    CI_GREEN,
    CI_PENDING,
    CI_RED,
    CI_UNKNOWN,
    VERDICT_FLAKY_CI,
    VERDICT_NOT_READY,
    VERDICT_READY,
    CIAssessment,
    CICheck,
    LearnedAdjustments,
    ReadinessInput,
    build_rebase_request,
    check_history_for,
    classify_ci,
    evaluate_merge_readiness,
    feature_tokens,
    flake_confidence,
    learned_adjustments_for,
    learned_rules_summary,
    normalize_reviews,
    note_rebase_outcome,
    record_merge_feedback,
    refresh_merge_readiness,
    refresh_review_and_ci_state,
    size_bucket,
    split_reviews,
    summarize_ci_status,
    touches_tests,
)
from tests.factories import (
    CICheckHistoryFactory,
    MergeReadinessFeedbackFactory,
    ProjectFactory,
    PullRequestFactory,
    ReviewDraftFactory,
    TestRunFactory,
)


def _check(name: str, conclusion: str = "success", status: str = "completed") -> dict[str, str]:
    return {"name": name, "status": status, "conclusion": conclusion, "url": f"https://ci/{name}"}


# --------------------------------------------------------------------------
# CI classification
# --------------------------------------------------------------------------


class TestCICheck:
    def test_from_dict_requires_name(self) -> None:
        assert CICheck.from_dict({"status": "completed"}) is None
        assert CICheck.from_dict({"name": ""}) is None

    def test_from_dict_defaults(self) -> None:
        check = CICheck.from_dict({"name": "build"})
        assert check is not None
        assert check.status == "completed"
        assert check.conclusion == ""
        assert not check.failed and not check.passed and not check.running

    def test_predicates(self) -> None:
        assert CICheck("a", conclusion="failure").failed
        assert CICheck("a", conclusion="timed_out").failed
        assert CICheck("a", conclusion="success").passed
        assert CICheck("a", conclusion="skipped").passed
        assert CICheck("a", status="in_progress").running


class TestSummarizeCIStatus:
    def test_empty(self) -> None:
        assert summarize_ci_status([]) == ""

    def test_fail_beats_pending(self) -> None:
        checks = [CICheck("a", conclusion="failure"), CICheck("b", status="queued")]
        assert summarize_ci_status(checks) == "fail"

    def test_pending(self) -> None:
        checks = [CICheck("a", conclusion="success"), CICheck("b", status="in_progress")]
        assert summarize_ci_status(checks) == "pending"

    def test_pass(self) -> None:
        assert summarize_ci_status([CICheck("a", conclusion="success")]) == "pass"


class TestFlakeConfidence:
    def test_configured_pattern_is_high(self) -> None:
        conf, why = flake_confidence(
            CICheck("pyspark-ml", conclusion="failure"), flaky_patterns=["pyspark-*"]
        )
        assert conf == 0.9
        assert "configured" in why

    def test_timed_out_is_likely_flaky(self) -> None:
        conf, why = flake_confidence(CICheck("integration", conclusion="timed_out"))
        assert conf == 0.7
        assert why == "timed out"

    def test_deterministic_name_is_low(self) -> None:
        conf, why = flake_confidence(CICheck("Lint / ruff", conclusion="failure"))
        assert conf == 0.1
        assert "don't flake" in why

    def test_unknown_check_default(self) -> None:
        conf, why = flake_confidence(CICheck("core tests", conclusion="failure"))
        assert conf == 0.35
        assert "no history" in why

    def test_history_pulls_toward_learned_rate(self) -> None:
        # Two confirmed flakes: learned = 3/4, weight = 2/4 → (0.35 + 0.75) / 2.
        conf, why = flake_confidence(
            CICheck("core tests", conclusion="failure"), history={"core tests": (2, 0)}
        )
        assert conf == pytest.approx(0.55)
        assert "flaked 2x" in why

    def test_real_marks_beat_a_configured_pattern_eventually(self) -> None:
        conf, _ = flake_confidence(
            CICheck("pyspark-ml", conclusion="failure"),
            flaky_patterns=["pyspark-*"],
            history={"pyspark-ml": (0, 4)},
        )
        assert conf < mr.FLAKE_THRESHOLD


class TestClassifyCI:
    def test_no_checks_is_unknown(self) -> None:
        result = classify_ci([])
        assert result.status == CI_UNKNOWN
        assert "no CI results" in result.reasons[0]

    def test_malformed_entries_are_dropped(self) -> None:
        result = classify_ci([{"status": "completed"}, _check("build")])
        assert result.total == 1
        assert result.status == CI_GREEN

    def test_all_green(self) -> None:
        result = classify_ci([_check("a"), _check("b", "skipped")])
        assert result.status == CI_GREEN
        assert result.failing == []

    def test_pending(self) -> None:
        result = classify_ci([_check("a"), _check("b", "", "in_progress")])
        assert result.status == CI_PENDING
        assert result.pending == ["b"]

    def test_deterministic_failure_is_red(self) -> None:
        result = classify_ci([_check("a"), _check("lint", "failure")])
        assert result.status == CI_RED
        assert result.failing == ["lint"]
        assert result.flake_confidence["lint"] == 0.1

    def test_timed_out_single_failure_is_flaky(self) -> None:
        result = classify_ci([_check("a"), _check("b"), _check("slow-suite", "timed_out")])
        assert result.status == CI_FLAKY
        assert "probably flaky" in result.reasons[0]

    def test_configured_flaky_pattern(self) -> None:
        result = classify_ci(
            [_check("a"), _check("Docker integration", "failure")],
            flaky_patterns=["docker*"],
        )
        assert result.status == CI_FLAKY

    def test_one_real_failure_makes_the_run_red(self) -> None:
        result = classify_ci(
            [_check("a"), _check("flaky-one", "timed_out"), _check("mypy", "failure")]
        )
        assert result.status == CI_RED

    def test_majority_failing_is_never_a_flake(self) -> None:
        checks = [_check(f"c{i}", "timed_out") for i in range(5)] + [_check("ok")]
        result = classify_ci(checks)
        assert result.status == CI_RED
        assert "not a flake" in result.reasons[0]

    def test_learned_history_flips_to_flaky(self) -> None:
        checks = [_check("a"), _check("core tests", "failure")]
        assert classify_ci(checks).status == CI_RED
        assert classify_ci(checks, history={"core tests": (3, 0)}).status == CI_FLAKY

    def test_learned_history_flips_to_real(self) -> None:
        checks = [_check("a"), _check("slow", "timed_out")]
        assert classify_ci(checks).status == CI_FLAKY
        assert classify_ci(checks, history={"slow": (0, 3)}).status == CI_RED

    def test_accepts_cicheck_instances(self) -> None:
        result = classify_ci([CICheck("a", conclusion="success")])
        assert result.status == CI_GREEN

    def test_round_trip_dict(self) -> None:
        result = classify_ci([_check("a"), _check("b", "timed_out")])
        again = CIAssessment.from_dict(result.to_dict())
        assert again.status == result.status
        assert again.failing == result.failing
        assert again.flake_confidence == {"b": 0.7}
        assert CIAssessment.from_dict(None).status == CI_UNKNOWN


# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------


def _green() -> CIAssessment:
    return classify_ci([_check("build")])


def _flaky() -> CIAssessment:
    return classify_ci([_check("build"), _check("slow", "timed_out")])


def _ready_input(**overrides: Any) -> ReadinessInput:
    base: dict[str, Any] = {
        "author": "alice",
        "operator_username": "holdenk",
        "title": "Fix a thing",
        "additions": 20,
        "deletions": 5,
        "changed_files": ["sql/core/Foo.scala", "sql/core/FooSuite.scala"],
        "review_states": {"bob": {"state": "APPROVED", "commit_id": "abc"}},
        "head_sha": "abc",
        "committers": ["bob"],
        "known_contributors": ["alice"],
        "review_ran": True,
        "days_since_update": 1.0,
        "ci": _green(),
        "mergeable": True,
    }
    base.update(overrides)
    return ReadinessInput(**base)


class TestEvaluateMergeReadiness:
    def test_happy_path_is_ready(self) -> None:
        result = evaluate_merge_readiness(_ready_input(), MergeReadinessConfig())
        assert result.verdict == VERDICT_READY
        assert result.listed
        assert result.blockers == []
        assert "CI green" in result.reasons
        assert "approved by bob" in result.reasons
        assert "committer approval from bob" in result.reasons
        assert result.score > 0.5
        assert result.approvers == ["bob"]

    def test_flaky_ci_is_its_own_verdict(self) -> None:
        result = evaluate_merge_readiness(_ready_input(ci=_flaky()), MergeReadinessConfig())
        assert result.verdict == VERDICT_FLAKY_CI
        assert any("probably a flake" in r for r in result.reasons)

    def test_red_ci_blocks(self) -> None:
        ci = classify_ci([_check("lint", "failure")])
        result = evaluate_merge_readiness(_ready_input(ci=ci), MergeReadinessConfig())
        assert result.verdict == VERDICT_NOT_READY
        assert "CI failing: lint" in result.blockers

    def test_pending_ci_blocks(self) -> None:
        ci = classify_ci([_check("a", "", "queued")])
        result = evaluate_merge_readiness(_ready_input(ci=ci), MergeReadinessConfig())
        assert "CI still running" in result.blockers

    def test_unknown_ci_blocks_only_when_required(self) -> None:
        ci = classify_ci([])
        assert "no CI results for this commit" in (
            evaluate_merge_readiness(_ready_input(ci=ci), MergeReadinessConfig()).blockers
        )
        relaxed = evaluate_merge_readiness(
            _ready_input(ci=ci), MergeReadinessConfig(require_ci=False)
        )
        assert "no CI results for this commit" not in relaxed.blockers

    @pytest.mark.parametrize(
        ("overrides", "blocker"),
        [
            ({"is_draft": True}, "draft / WIP"),
            ({"title": "[WIP] thing"}, "draft / WIP"),
            ({"mergeable": False}, "merge conflicts"),
            ({"dismissed_sha": "abc"}, "you marked this not ready"),
            (
                {"review_states": {"carol": {"state": "CHANGES_REQUESTED", "commit_id": "abc"}}},
                "changes requested by carol",
            ),
            ({"additions": 3000}, "too big"),
            ({"days_since_update": 120.0}, "no activity for 120 days"),
            ({"open_findings": {"critical": 1, "important": 1}}, "2 unresolved"),
            ({"test_verdict": "broken"}, "differential tests fail"),
        ],
    )
    def test_blockers(self, overrides: dict[str, Any], blocker: str) -> None:
        result = evaluate_merge_readiness(_ready_input(**overrides), MergeReadinessConfig())
        assert result.verdict == VERDICT_NOT_READY
        assert any(blocker in b for b in result.blockers), result.blockers

    def test_staleness_can_be_disabled(self) -> None:
        result = evaluate_merge_readiness(
            _ready_input(days_since_update=400.0), MergeReadinessConfig(stale_after_days=0)
        )
        assert not any("no activity" in b for b in result.blockers)

    def test_stale_approval_is_half_credit(self) -> None:
        result = evaluate_merge_readiness(
            _ready_input(review_states={"bob": {"state": "APPROVED", "commit_id": "old"}}),
            MergeReadinessConfig(),
        )
        assert result.stale_approvers == ["bob"]
        assert result.breakdown["approved"] == mr.READINESS_WEIGHTS["approved"] / 2
        assert any("before the latest push" in r for r in result.reasons)

    def test_require_committer_approval(self) -> None:
        result = evaluate_merge_readiness(
            _ready_input(review_states={"dave": {"state": "APPROVED", "commit_id": "abc"}}),
            MergeReadinessConfig(require_committer_approval=True),
        )
        assert "no committer approval yet" in result.blockers

    def test_operator_approval_counts_extra(self) -> None:
        result = evaluate_merge_readiness(
            _ready_input(
                review_states={"holdenk": {"state": "APPROVED", "commit_id": "abc"}},
            ),
            MergeReadinessConfig(),
        )
        assert "operator_approved" in result.breakdown
        assert any("you already approved" in r for r in result.reasons)

    def test_new_contributor_is_docked_not_blocked(self) -> None:
        result = evaluate_merge_readiness(
            _ready_input(author="newbie", known_contributors=[], is_new_contributor=True),
            MergeReadinessConfig(),
        )
        assert result.breakdown["new_contributor"] < 0
        assert any("new contributor" in r for r in result.reasons)

    def test_committer_author(self) -> None:
        result = evaluate_merge_readiness(_ready_input(author="bob"), MergeReadinessConfig())
        assert "committer_author" in result.breakdown

    def test_operator_counts_as_known(self) -> None:
        result = evaluate_merge_readiness(
            _ready_input(author="holdenk", known_contributors=[]), MergeReadinessConfig()
        )
        assert "known_contributor" in result.breakdown

    def test_test_verdicts_and_nits(self) -> None:
        good = evaluate_merge_readiness(_ready_input(test_verdict="good"), MergeReadinessConfig())
        assert "tests_good" in good.breakdown
        suspect = evaluate_merge_readiness(
            _ready_input(test_verdict="suspect", open_findings={"nit": 2}),
            MergeReadinessConfig(),
        )
        assert "tests_suspect" in suspect.breakdown
        assert "open_nits" in suspect.breakdown

    def test_medium_diff_and_no_review_run(self) -> None:
        result = evaluate_merge_readiness(
            _ready_input(additions=300, review_ran=False), MergeReadinessConfig()
        )
        assert "medium_diff" in result.breakdown
        assert "clean_review" not in result.breakdown

    def test_below_threshold_names_the_bar(self) -> None:
        result = evaluate_merge_readiness(
            _ready_input(
                review_states={},
                review_ran=False,
                author="stranger",
                known_contributors=[],
                is_new_contributor=True,
                changed_files=["docs/x.md"],
                days_since_update=30.0,
            ),
            MergeReadinessConfig(),
        )
        assert result.verdict == VERDICT_NOT_READY
        assert any("below the 50 bar" in b for b in result.blockers)

    def test_learned_dismissals_dock_points(self) -> None:
        learned = LearnedAdjustments(feature_rates={"author:alice": (3, 4)})
        result = evaluate_merge_readiness(_ready_input(learned=learned), MergeReadinessConfig())
        assert result.breakdown["learned_dismissals"] < 0
        assert any("sent back 3 of 4" in r for r in result.reasons)

    def test_learned_needs_tests_blocks_prs_without_tests(self) -> None:
        learned = LearnedAdjustments(reason_counts={"needs-tests": 3})
        with_tests = evaluate_merge_readiness(_ready_input(learned=learned), MergeReadinessConfig())
        assert with_tests.verdict == VERDICT_READY
        without = evaluate_merge_readiness(
            _ready_input(learned=learned, changed_files=["sql/core/Foo.scala"]),
            MergeReadinessConfig(),
        )
        assert any("no test changes" in b for b in without.blockers)

    def test_learned_needs_committer_review_blocks(self) -> None:
        learned = LearnedAdjustments(reason_counts={"needs-committer-review": 3})
        result = evaluate_merge_readiness(
            _ready_input(
                learned=learned,
                review_states={"dave": {"state": "APPROVED", "commit_id": "abc"}},
            ),
            MergeReadinessConfig(),
        )
        assert any("no committer approval (you've sent back 3" in b for b in result.blockers)

    def test_to_detail_round_trips_lists(self) -> None:
        result = evaluate_merge_readiness(_ready_input(), MergeReadinessConfig())
        detail = result.to_detail()
        assert detail["approvers"] == ["bob"]
        assert isinstance(detail["ci"], dict)
        assert "evaluated_at" in detail


class TestSplitReviews:
    def test_split(self) -> None:
        states = {
            "a": {"state": "APPROVED", "commit_id": "head"},
            "b": {"state": "APPROVED", "commit_id": "old"},
            "c": {"state": "CHANGES_REQUESTED", "commit_id": "head"},
            "d": {"state": "APPROVED", "commit_id": ""},
        }
        approvers, stale, changes = split_reviews(states, "head")
        assert approvers == ["a", "d"]
        assert stale == ["b"]
        assert changes == ["c"]


class TestNormalizeReviews:
    def test_latest_state_wins_and_noise_is_ignored(self) -> None:
        raw = [
            {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED", "commit_id": "1"},
            {"user": {"login": "bob"}, "state": "COMMENTED", "commit_id": "2"},
            {"user": {"login": "bob"}, "state": "APPROVED", "commit_id": "3", "submitted_at": "t"},
            {"user": {"login": "alice"}, "state": "APPROVED", "commit_id": "3"},
            {"user": {"login": "some-app[bot]"}, "state": "APPROVED", "commit_id": "3"},
            {"user": {"login": "carol"}, "state": "APPROVED", "commit_id": "3"},
            {"user": {"login": "carol"}, "state": "DISMISSED", "commit_id": "3"},
            {"user": {"login": "dave"}, "state": "REQUEST_CHANGES", "commit_id": "3"},
            {"user": None, "state": "APPROVED"},
            {"state": "PENDING"},
        ]
        states = normalize_reviews(raw, author="alice")
        assert states == {
            "bob": {"state": "APPROVED", "commit_id": "3", "submitted_at": "t"},
            "dave": {"state": "CHANGES_REQUESTED", "commit_id": "3", "submitted_at": ""},
        }


class TestFeatureHelpers:
    def test_size_bucket(self) -> None:
        assert size_bucket(10, 10) == "small"
        assert size_bucket(300, 0) == "medium"
        assert size_bucket(600, 0) == "large"
        assert size_bucket(1000, 0) == "huge"

    def test_feature_tokens(self) -> None:
        tokens = feature_tokens("Alice", ["Bug"], ["sql/x.scala", "sql/y.scala", "README.md"], 1, 1)
        assert tokens == ["author:alice", "label:bug", "path:(root)", "path:sql", "size:small"]
        assert feature_tokens("", [], [], 0, 0) == ["size:small"]

    def test_touches_tests(self) -> None:
        assert touches_tests(["a/FooSuite.scala"])  # Spark's naming, no "test" in the path
        assert touches_tests(["tests/x.py"])
        assert touches_tests(["spec/x_spec.rb"])
        assert not touches_tests(["src/main.py", "sql/core/Foo.scala"])


class TestLearnedAdjustments:
    def test_worst_feature_needs_enough_observations(self) -> None:
        learned = LearnedAdjustments(
            feature_rates={"author:a": (2, 2), "path:sql": (3, 4), "label:x": (5, 5)}
        )
        assert learned.worst_feature(["author:a"]) is None
        assert learned.worst_feature(["path:sql", "label:x"]) == ("label:x", 5, 5)
        assert learned.worst_feature(["path:docs"]) is None

    def test_blocker_reasons_and_summary(self) -> None:
        learned = LearnedAdjustments(
            feature_rates={"author:a": (3, 3)},
            reason_counts={"needs-tests": 3, "design": 5, "needs-committer-review": 1},
        )
        assert learned.blocker_reasons() == ["needs-tests"]
        lines = learned.summary_lines()
        assert any("needs-tests" in line for line in lines)
        assert any("author:a" in line for line in lines)
        assert LearnedAdjustments().summary_lines() == []


# --------------------------------------------------------------------------
# Django half
# --------------------------------------------------------------------------


@pytest.fixture
def project_config() -> ProjectConfig:
    return ProjectConfig(owner="apache", repo="spark", committers=["bob"])


@pytest.mark.django_db
class TestRefreshReviewAndCIState:
    def test_fetches_reviews_and_checks(self, project_config: ProjectConfig) -> None:
        pr = PullRequestFactory(
            project=ProjectFactory(owner="apache", repo="spark"), author="alice", head_sha="h1"
        )
        client = MagicMock()
        client.list_pull_request_reviews.return_value = [
            {"user": {"login": "bob"}, "state": "APPROVED", "commit_id": "h1"}
        ]
        client.get_commit_checks.return_value = [_check("build"), _check("slow", "timed_out")]
        refresh_review_and_ci_state(client, pr, project_config)
        assert pr.review_states["bob"]["state"] == "APPROVED"
        assert pr.approval_count == 1
        assert pr.ci_status == "fail"
        assert pr.ci_checks_sha == "h1"
        assert [c["name"] for c in pr.ci_checks] == ["build", "slow"]
        client.get_commit_checks.assert_called_once_with("apache", "spark", "h1")

    def test_unsupported_forge_logs_once(
        self, project_config: ProjectConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        pr = PullRequestFactory(head_sha="h1", review_states={"x": {"state": "APPROVED"}})
        client = MagicMock()
        client.list_pull_request_reviews.side_effect = NotImplementedError("no")
        mr._warned_unsupported.discard("MagicMock")
        with caplog.at_level(logging.INFO, logger="franktheunicorn.scoring.merge_readiness"):
            refresh_review_and_ci_state(client, pr, project_config)
            refresh_review_and_ci_state(client, pr, project_config)
        notices = [r for r in caplog.records if "no review/CI data" in r.getMessage()]
        assert len(notices) == 1
        # Nothing was touched and the checks call was never made.
        assert pr.review_states == {"x": {"state": "APPROVED"}}
        client.get_commit_checks.assert_not_called()

    def test_review_failure_keeps_state_and_still_fetches_checks(
        self, project_config: ProjectConfig
    ) -> None:
        pr = PullRequestFactory(head_sha="h1", review_states={"x": {"state": "APPROVED"}})
        client = MagicMock()
        client.list_pull_request_reviews.side_effect = RuntimeError("rate limited")
        client.get_commit_checks.return_value = [_check("build")]
        refresh_review_and_ci_state(client, pr, project_config)
        assert pr.review_states == {"x": {"state": "APPROVED"}}
        assert pr.ci_status == "pass"

    def test_checks_failure_keeps_previous(self, project_config: ProjectConfig) -> None:
        pr = PullRequestFactory(
            head_sha="h1", ci_checks_sha="h1", ci_status="pass", ci_checks=[_check("old")]
        )
        client = MagicMock()
        client.list_pull_request_reviews.return_value = []
        client.get_commit_checks.side_effect = RuntimeError("boom")
        refresh_review_and_ci_state(client, pr, project_config)
        assert pr.ci_status == "pass"
        assert pr.ci_checks[0]["name"] == "old"

    def test_checks_unsupported_is_quiet(self, project_config: ProjectConfig) -> None:
        pr = PullRequestFactory(head_sha="h1")
        client = MagicMock()
        client.list_pull_request_reviews.return_value = []
        client.get_commit_checks.side_effect = NotImplementedError("no")
        refresh_review_and_ci_state(client, pr, project_config)
        assert pr.ci_checks == []

    def test_no_head_skips_checks(self, project_config: ProjectConfig) -> None:
        pr = PullRequestFactory(head_sha="")
        client = MagicMock()
        client.list_pull_request_reviews.return_value = []
        refresh_review_and_ci_state(client, pr, project_config)
        client.get_commit_checks.assert_not_called()


@pytest.mark.django_db
class TestNoteRebaseOutcome:
    def _pr(self, **kwargs: Any) -> PullRequest:
        defaults: dict[str, Any] = {
            "head_sha": "new",
            "ci_checks_sha": "new",
            "rebase_requested_sha": "old",
            "rebase_requested_at": timezone.now(),
            "rebase_requested_checks": ["slow", "docker"],
        }
        defaults.update(kwargs)
        return PullRequestFactory(**defaults)

    def test_green_confirms_flake_red_confirms_real(self) -> None:
        pr = self._pr()
        note_rebase_outcome(
            pr, [CICheck("slow", conclusion="success"), CICheck("docker", conclusion="failure")]
        )
        rows = {r.check_name: r for r in CICheckHistory.objects.filter(project=pr.project)}
        assert rows["slow"].flaky_count == 1 and rows["slow"].real_count == 0
        assert rows["docker"].real_count == 1 and rows["docker"].last_failed_at is not None
        assert pr.rebase_requested_sha == ""
        assert pr.rebase_requested_at is None
        assert pr.rebase_requested_checks == []

    def test_still_running_waits(self) -> None:
        pr = self._pr()
        note_rebase_outcome(
            pr, [CICheck("slow", status="in_progress"), CICheck("docker", conclusion="success")]
        )
        assert CICheckHistory.objects.count() == 0
        assert pr.rebase_requested_sha == "old"

    def test_vanished_checks_clear_without_learning(self) -> None:
        pr = self._pr()
        note_rebase_outcome(pr, [CICheck("unrelated", conclusion="success")])
        assert CICheckHistory.objects.count() == 0
        assert pr.rebase_requested_sha == ""

    def test_same_head_or_stale_checks_do_nothing(self) -> None:
        pr = self._pr(head_sha="old")
        note_rebase_outcome(pr, [CICheck("slow", conclusion="success")])
        assert pr.rebase_requested_sha == "old"
        pr2 = self._pr(ci_checks_sha="older")
        note_rebase_outcome(pr2, [CICheck("slow", conclusion="success")])
        assert pr2.rebase_requested_sha == "old"
        assert CICheckHistory.objects.count() == 0


@pytest.mark.django_db
class TestRefreshMergeReadiness:
    def test_stores_verdict_and_logs_when_listed(
        self, project_config: ProjectConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        project = ProjectFactory(owner="apache", repo="spark", contributors_cache=["alice"])
        pr = PullRequestFactory(
            project=project,
            author="alice",
            head_sha="h1",
            ci_checks_sha="h1",
            mergeable=True,
            additions=10,
            deletions=2,
            changed_files=["sql/FooSuite.scala"],
            review_states={"bob": {"state": "APPROVED", "commit_id": "h1"}},
            ci_checks=[_check("build")],
            github_updated_at=timezone.now() - timedelta(days=1),
            agent_runs={"agent": {"status": "ok"}},
        )
        TestRunFactory(pull_request=pr, status="completed", differential_verdict="good")
        with caplog.at_level(logging.INFO, logger="franktheunicorn.scoring.merge_readiness"):
            result = refresh_merge_readiness(pr, project_config, "holdenk")
        assert result.verdict == VERDICT_READY
        pr.refresh_from_db()
        assert pr.merge_readiness_verdict == VERDICT_READY
        assert pr.merge_readiness_score == result.score
        assert pr.merge_readiness_detail["approvers"] == ["bob"]
        assert "tests_good" in pr.merge_readiness_detail["breakdown"]
        assert any("almost ready to merge" in r.getMessage() for r in caplog.records)

    def test_open_serious_findings_block(self, project_config: ProjectConfig) -> None:
        pr = PullRequestFactory(
            head_sha="h1",
            ci_checks_sha="h1",
            mergeable=True,
            ci_checks=[_check("build")],
            review_states={"bob": {"state": "APPROVED", "commit_id": "h1"}},
        )
        ReviewDraftFactory(pull_request=pr, severity="critical", status="pending")
        ReviewDraftFactory(pull_request=pr, severity="critical", status="posted")
        ReviewDraftFactory(pull_request=pr, severity="important", is_auto_suppressed=True)
        result = refresh_merge_readiness(pr, project_config, "holdenk", save=False)
        assert any("1 unresolved critical" in b for b in result.blockers)
        pr.refresh_from_db()
        assert pr.merge_readiness_detail == {}  # save=False

    def test_uses_learned_history_from_db(self, project_config: ProjectConfig) -> None:
        project = ProjectFactory(owner="apache", repo="spark", contributors_cache=["alice"])
        CICheckHistoryFactory(project=project, check_name="core", flaky_count=4)
        pr = PullRequestFactory(
            project=project,
            author="alice",
            head_sha="h1",
            ci_checks_sha="h1",
            mergeable=True,
            ci_checks=[_check("build"), _check("core", "failure")],
            review_states={"bob": {"state": "APPROVED", "commit_id": "h1"}},
            github_updated_at=timezone.now(),
            changed_files=["x/FooSuite.scala"],
        )
        result = refresh_merge_readiness(pr, project_config, "holdenk")
        assert result.ci.status == CI_FLAKY
        assert result.verdict == VERDICT_FLAKY_CI
        assert check_history_for(project) == {"core": (4, 0)}


@pytest.mark.django_db
class TestRecordMergeFeedback:
    def _candidate(self) -> PullRequest:
        return PullRequestFactory(
            author="alice",
            head_sha="h1",
            labels=["bug"],
            changed_files=["sql/x.scala"],
            merge_readiness_score=0.7,
            merge_readiness_detail={
                "ci": {"status": "flaky", "failing": ["slow", "docker"]},
                "approvers": ["bob"],
            },
        )

    def test_not_ready_parks_and_records(self) -> None:
        pr = self._candidate()
        fb = record_merge_feedback(
            pr,
            "not-ready",
            reason_code="needs-tests",
            reason_text="no suite",
            operator_username="h",
        )
        assert fb.project == pr.project
        assert fb.head_sha == "h1"
        assert fb.features["tokens"] == ["author:alice", "label:bug", "path:sql", "size:small"]
        assert fb.features["failing_checks"] == ["slow", "docker"]
        assert fb.features["approvers"] == ["bob"]
        pr.refresh_from_db()
        assert pr.merge_readiness_dismissed_sha == "h1"
        action = OperatorAction.objects.get()
        assert action.action_type == "dismiss_merge_candidate"
        assert action.notes == "needs-tests: no suite"
        assert CICheckHistory.objects.count() == 0

    def test_not_a_flake_marks_checks_real(self) -> None:
        pr = self._candidate()
        record_merge_feedback(pr, "not-ready", reason_code="not-a-flake")
        rows = {r.check_name: r.real_count for r in CICheckHistory.objects.all()}
        assert rows == {"slow": 1, "docker": 1}

    def test_lgtm_records_operator_approval(self) -> None:
        pr = self._candidate()
        record_merge_feedback(pr, "lgtm", operator_username="holdenk")
        pr.refresh_from_db()
        assert pr.review_states["holdenk"]["state"] == "APPROVED"
        assert pr.review_states["holdenk"]["commit_id"] == "h1"
        assert pr.approval_count == 1
        assert OperatorAction.objects.get().action_type == "lgtm_pr"
        assert OperatorAction.objects.get().notes == ""

    def test_rebase_requested_remembers_checks(self) -> None:
        pr = self._candidate()
        record_merge_feedback(pr, "rebase-requested")
        pr.refresh_from_db()
        assert pr.rebase_requested_sha == "h1"
        assert pr.rebase_requested_at is not None
        assert pr.rebase_requested_checks == ["slow", "docker"]
        assert OperatorAction.objects.get().action_type == "request_rebase"

    def test_merged_is_just_the_record(self) -> None:
        pr = self._candidate()
        record_merge_feedback(pr, "merged")
        assert MergeReadinessFeedback.objects.get().verdict == "merged"
        assert OperatorAction.objects.get().action_type == "merge_pr"
        pr.refresh_from_db()
        assert pr.merge_readiness_dismissed_sha == ""

    def test_handles_missing_detail(self) -> None:
        pr = PullRequestFactory(merge_readiness_detail={})
        fb = record_merge_feedback(pr, "not-ready", reason_code="other")
        assert fb.features["failing_checks"] == []
        assert fb.features["ci_status"] == CI_UNKNOWN


@pytest.mark.django_db
class TestLearnedFromDB:
    def test_learned_adjustments_for_aggregates(self) -> None:
        project = ProjectFactory()
        MergeReadinessFeedbackFactory(
            project=project,
            verdict="not-ready",
            reason_code="needs-tests",
            features={"tokens": ["author:a", "path:sql"]},
        )
        MergeReadinessFeedbackFactory(
            project=project,
            verdict="not-ready",
            reason_code="needs-tests",
            features={"tokens": ["author:a"]},
        )
        MergeReadinessFeedbackFactory(
            project=project, verdict="lgtm", features={"tokens": ["author:a", "path:sql"]}
        )
        MergeReadinessFeedbackFactory(
            project=project, verdict="rebase-requested", features={"tokens": ["author:a"]}
        )
        MergeReadinessFeedbackFactory(project=project, verdict="not-ready", features={})
        MergeReadinessFeedbackFactory(project=ProjectFactory(), verdict="not-ready")
        learned = learned_adjustments_for(project)
        assert learned.feature_rates == {"author:a": (2, 3), "path:sql": (1, 2)}
        assert learned.reason_counts == {"needs-tests": 2}

    def test_learned_rules_summary_includes_checks(self) -> None:
        project = ProjectFactory()
        CICheckHistoryFactory(project=project, check_name="slow", flaky_count=3)
        CICheckHistoryFactory(project=project, check_name="lint", real_count=2)
        CICheckHistoryFactory(project=project, check_name="empty")
        for _ in range(3):
            MergeReadinessFeedbackFactory(
                project=project, verdict="not-ready", reason_code="needs-tests", features={}
            )
        lines = learned_rules_summary(project)
        assert any("needs-tests" in line for line in lines)
        assert any(line.startswith("slow: probably flaky") for line in lines)
        assert any(line.startswith("lint: treated as real") for line in lines)
        assert not any(line.startswith("empty:") for line in lines)


@pytest.mark.django_db
class TestBuildRebaseRequest:
    def test_default_template(self) -> None:
        pr = PullRequestFactory(
            author="alice",
            base_branch="branch-4.0",
            merge_readiness_detail={"ci": {"status": "flaky", "failing": ["slow", "docker"]}},
        )
        body = build_rebase_request(pr, MergeReadinessConfig())
        assert body.startswith("Hi @alice")
        assert "`slow`, `docker`" in body
        assert "`branch-4.0`" in body

    def test_no_failing_checks_and_no_base(self) -> None:
        pr = PullRequestFactory(author="alice", base_branch="")
        body = build_rebase_request(pr, MergeReadinessConfig())
        assert "failed on CI," in body
        assert "`main`" in body

    def test_bad_template_falls_back(self, caplog: pytest.LogCaptureFixture) -> None:
        pr = PullRequestFactory(author="alice")
        cfg = MergeReadinessConfig(rebase_request_template="Hi {nope}")
        with caplog.at_level(logging.WARNING):
            body = build_rebase_request(pr, cfg)
        assert body == DEFAULT_REBASE_REQUEST_TEMPLATE.format(
            author="alice", checks="CI", base_branch="main"
        )
        assert "unknown placeholder" in caplog.text


class TestMergeReadinessConfig:
    def test_defaults(self) -> None:
        cfg = MergeReadinessConfig()
        assert cfg.enabled is True
        assert cfg.ready_threshold == 0.5
        assert "*lint*" in cfg.deterministic_checks
        assert "{author}" in cfg.rebase_request_template

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"ready_threshold": 1.5},
            {"max_diff_lines": 0},
            {"stale_after_days": -1},
            {"rebase_request_template": "   "},
        ],
    )
    def test_validators(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="merge_readiness"):
            MergeReadinessConfig(**kwargs)

    def test_project_config_carries_it(self) -> None:
        pc = ProjectConfig(owner="o", repo="r", merge_readiness={"flaky_checks": ["docker*"]})
        assert pc.merge_readiness.flaky_checks == ["docker*"]


def test_datetime_import_used() -> None:
    # Keeps the UTC/datetime imports honest for the Django half above.
    assert datetime.now(tz=UTC).tzinfo is UTC


class TestInconclusiveConclusions:
    """A ``stale`` (or unknown) completed conclusion is not a pass."""

    def test_stale_only_is_not_green(self) -> None:
        result = classify_ci([_check("build", "stale")])
        assert result.status == CI_PENDING
        assert result.pending == ["build"]
        assert any("inconclusive" in r and "stale" in r for r in result.reasons)

    def test_unknown_conclusion_is_not_green(self) -> None:
        result = classify_ci([_check("a"), _check("b", "something_new")])
        assert result.status == CI_PENDING
        assert "b" in result.pending

    def test_failure_still_wins(self) -> None:
        result = classify_ci([_check("a", "stale"), _check("lint", "failure")])
        assert result.status == CI_RED

    def test_summarize_ci_status_treats_stale_as_pending(self) -> None:
        assert summarize_ci_status([CICheck("a", conclusion="stale")]) == "pending"
        assert CICheck("a", conclusion="stale").inconclusive
        assert not CICheck("a", conclusion="success").inconclusive
        assert not CICheck("a", status="queued").inconclusive


@pytest.mark.django_db
class TestChecksForAnotherHead:
    """CI fetched for the previous commit must not vouch for a new one."""

    def test_current_checks_requires_matching_sha(self) -> None:
        pr = PullRequestFactory(head_sha="new", ci_checks_sha="old", ci_checks=[_check("build")])
        assert mr.current_checks(pr) == []
        pr.ci_checks_sha = "new"
        assert [c["name"] for c in mr.current_checks(pr)] == ["build"]

    def test_readiness_treats_mismatched_checks_as_unknown(
        self, project_config: ProjectConfig
    ) -> None:
        project = ProjectFactory(owner="apache", repo="spark", contributors_cache=["alice"])
        pr = PullRequestFactory(
            project=project,
            author="alice",
            head_sha="new",
            ci_checks_sha="old",
            ci_checks=[_check("build")],
            mergeable=True,
            review_states={"bob": {"state": "APPROVED", "commit_id": "new"}},
            github_updated_at=timezone.now(),
            changed_files=["x/FooSuite.scala"],
        )
        result = refresh_merge_readiness(pr, project_config, "holdenk")
        assert result.ci.status == CI_UNKNOWN
        assert result.verdict == VERDICT_NOT_READY
        assert "no CI results for this commit" in result.blockers
        assert any("older commit" in r for r in result.ci.reasons)

    def test_failed_fetch_after_a_push_clears_ci_status(
        self, project_config: ProjectConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        pr = PullRequestFactory(
            head_sha="new", ci_checks_sha="old", ci_status="pass", ci_checks=[_check("build")]
        )
        client = MagicMock()
        client.list_pull_request_reviews.return_value = []
        client.get_commit_checks.side_effect = RuntimeError("rate limited")
        with caplog.at_level(logging.WARNING):
            refresh_review_and_ci_state(client, pr, project_config)
        assert pr.ci_status == ""
        assert pr.ci_checks_sha == "old"  # kept, labelled, just not trusted for this head
        assert pr.ci_checks[0]["name"] == "build"
        assert "older commit" in caplog.text

    def test_failed_fetch_on_the_same_head_keeps_status(
        self, project_config: ProjectConfig
    ) -> None:
        pr = PullRequestFactory(head_sha="h1", ci_checks_sha="h1", ci_status="pass")
        client = MagicMock()
        client.list_pull_request_reviews.return_value = []
        client.get_commit_checks.side_effect = RuntimeError("boom")
        refresh_review_and_ci_state(client, pr, project_config)
        assert pr.ci_status == "pass"


@pytest.mark.django_db
class TestClearMergeReadiness:
    def test_clears_a_stored_verdict(self, caplog: pytest.LogCaptureFixture) -> None:
        pr = PullRequestFactory(
            merge_readiness_verdict=VERDICT_READY,
            merge_readiness_score=0.8,
            merge_readiness_detail={"reasons": ["CI green"]},
        )
        with caplog.at_level(logging.INFO, logger="franktheunicorn.scoring.merge_readiness"):
            mr.clear_merge_readiness(pr)
        assert pr.merge_readiness_verdict == VERDICT_NOT_READY
        assert pr.merge_readiness_score == 0.0
        assert pr.merge_readiness_detail == {}
        assert "cleared its verdict" in caplog.text

    def test_nothing_to_clear_is_quiet(self, caplog: pytest.LogCaptureFixture) -> None:
        pr = PullRequestFactory()
        with caplog.at_level(logging.INFO, logger="franktheunicorn.scoring.merge_readiness"):
            mr.clear_merge_readiness(pr)
        assert "cleared" not in caplog.text
