"""Tests for the worthiness screen behind a project's minimize_reviews flag."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import (
    LLMBackendConfig,
    OperatorConfig,
    ProjectConfig,
    ReviewWorthinessConfig,
)
from franktheunicorn.review.backends.base import BaseLLMBackend
from franktheunicorn.review.mergeability import LIKELY_MERGEABLE, Triage, record_triage
from franktheunicorn.review.worthiness import (
    NOT_WORTH_REVIEWING,
    UNCLEAR,
    WORTH_REVIEWING,
    Worthiness,
    current_worthiness,
    looks_close_to_mergeable,
    parse_worthiness,
    record_worthiness,
    run_worthiness_screen,
    worthiness_backend_configs,
)
from tests.factories import PullRequestFactory


def _op(**kw: Any) -> OperatorConfig:
    params: dict[str, Any] = {
        "github_username": "holdenk",
        "mock_mode": True,
        "llm_backends": [LLMBackendConfig(provider="openai", model="qwen")],
    }
    params.update(kw)
    return OperatorConfig(**params)


class TestParseWorthiness:
    def test_reads_the_last_object(self) -> None:
        raw = 'noise {"verdict": "worth-reviewing"} really:\n{"verdict": "not-worth-reviewing", "reason": "typo fixes"}'
        worthiness = parse_worthiness(raw)
        assert worthiness == Worthiness(NOT_WORTH_REVIEWING, "typo fixes")

    def test_an_unknown_verdict_is_no_answer(self) -> None:
        assert parse_worthiness('{"verdict": "ship it"}') is None

    def test_unclear_is_a_real_verdict(self) -> None:
        worthiness = parse_worthiness('{"verdict": "unclear", "reason": "cannot tell"}')
        assert worthiness is not None
        assert worthiness.verdict == UNCLEAR
        assert not worthiness.worth_reviewing

    def test_worth_reviewing_is_the_only_yes(self) -> None:
        assert Worthiness(WORTH_REVIEWING).worth_reviewing is True
        assert Worthiness(NOT_WORTH_REVIEWING).worth_reviewing is False
        assert Worthiness(UNCLEAR).worth_reviewing is False


@pytest.mark.django_db
class TestLooksCloseToMergeable:
    def test_nothing_yet(self) -> None:
        pr = PullRequestFactory()
        assert looks_close_to_mergeable(pr) == ""

    def test_merge_intent_counts(self) -> None:
        pr = PullRequestFactory(merge_intent="approved")
        assert looks_close_to_mergeable(pr)

    def test_approval_count_counts(self) -> None:
        pr = PullRequestFactory(approval_count=1)
        assert looks_close_to_mergeable(pr)

    def test_cached_likely_mergeable_triage_counts(self) -> None:
        pr = PullRequestFactory(head_sha="abc")
        record_triage(pr, Triage(LIKELY_MERGEABLE, "small", source="x"))
        assert looks_close_to_mergeable(pr)


class TestBackendConfigs:
    def test_explicit_backend_wins(self) -> None:
        cheap = LLMBackendConfig(provider="openai", model="qwen3-8b")
        oc = _op(review_worthiness=ReviewWorthinessConfig(backend=cheap))
        assert worthiness_backend_configs(oc) == [cheap]

    def test_unset_falls_back_to_llm_backends(self) -> None:
        oc = _op()
        assert worthiness_backend_configs(oc) == list(oc.llm_backends)


@pytest.mark.django_db
class TestRunWorthinessScreen:
    def test_the_explicit_cheap_backend_answers(self) -> None:
        pr = PullRequestFactory(head_sha="abc")
        working = MagicMock(spec=BaseLLMBackend)
        working.metered_call.return_value = (
            '{"verdict": "worth-reviewing", "reason": "risky core change"}'
        )
        cheap = LLMBackendConfig(provider="openai", model="cheap")
        oc = OperatorConfig(
            github_username="holdenk",
            mock_mode=True,
            llm_backends=[LLMBackendConfig(provider="openai", model="expensive")],
            review_worthiness=ReviewWorthinessConfig(backend=cheap),
        )
        with patch("franktheunicorn.review.backends.get_backend", return_value=working) as gb:
            worthiness = run_worthiness_screen(pr, ProjectConfig(owner="a", repo="b"), oc, "d")
        assert worthiness is not None
        assert worthiness.worth_reviewing is True
        assert worthiness.reason == "risky core change"
        assert worthiness.source == "openai/cheap"
        # The expensive review backend is never constructed for the screen.
        assert gb.call_count == 1

    def test_falls_back_to_llm_backends_in_order(self) -> None:
        pr = PullRequestFactory(head_sha="abc")
        broken = MagicMock(spec=BaseLLMBackend)
        broken.metered_call.side_effect = RuntimeError("down")
        working = MagicMock(spec=BaseLLMBackend)
        working.metered_call.return_value = '{"verdict": "not-worth-reviewing", "reason": "typo"}'
        oc = _op(
            llm_backends=[
                LLMBackendConfig(provider="openai", model="first"),
                LLMBackendConfig(provider="openai", model="second"),
            ]
        )
        with patch("franktheunicorn.review.backends.get_backend", side_effect=[broken, working]):
            worthiness = run_worthiness_screen(pr, ProjectConfig(owner="a", repo="b"), oc, "d")
        assert worthiness is not None
        assert worthiness.worth_reviewing is False
        assert worthiness.source == "openai/second"

    def test_no_answer_from_anyone_is_none(self) -> None:
        pr = PullRequestFactory(head_sha="abc")
        with patch("franktheunicorn.review.backends.get_backend", return_value=None):
            worthiness = run_worthiness_screen(pr, ProjectConfig(owner="a", repo="b"), _op(), "d")
        assert worthiness is None


@pytest.mark.django_db
class TestRecordAndCurrent:
    def test_record_is_per_head_and_state(self) -> None:
        pr = PullRequestFactory(head_sha="abc", ci_status="success")
        record_worthiness(pr, Worthiness(NOT_WORTH_REVIEWING, "docs only", source="x"))
        pr.refresh_from_db()
        assert current_worthiness(pr) == Worthiness(NOT_WORTH_REVIEWING, "docs only", source="x")

        # A push re-screens: the verdict was for the old head.
        pr.head_sha = "def"
        assert current_worthiness(pr) is None

    def test_a_state_change_without_a_push_rescreens(self) -> None:
        pr = PullRequestFactory(head_sha="abc", ci_status="pending")
        record_worthiness(pr, Worthiness(NOT_WORTH_REVIEWING, "chore"))
        pr.refresh_from_db()
        assert current_worthiness(pr) is not None
        # CI finished red since the screen ran — the answer may be different now.
        pr.ci_status = "failure"
        assert current_worthiness(pr) is None
