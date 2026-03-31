"""Tests for confidence-gated auto-posting (v1.5 triple gate)."""

from __future__ import annotations

import pytest

from franktheunicorn.config.models import PostingConfig, ProjectConfig
from franktheunicorn.review.auto_poster import should_auto_post
from tests.factories import PullRequestFactory, ReviewDraftFactory


@pytest.mark.django_db
class TestShouldAutoPost:
    def _make_project_config(self, mode: str = "confidence-gated") -> ProjectConfig:
        return ProjectConfig(
            owner="apache",
            repo="spark",
            posting=PostingConfig(mode=mode, confidence_threshold=0.85),
        )

    def test_passes_all_gates(self) -> None:
        config = self._make_project_config()
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            confidence=0.9,
            tone_guard_applied=True,
        )
        assert should_auto_post(draft, config) is True

    def test_gate1_draft_only_mode(self) -> None:
        config = self._make_project_config(mode="draft-only")
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            confidence=0.9,
            tone_guard_applied=True,
        )
        assert should_auto_post(draft, config) is False

    def test_gate2_low_confidence(self) -> None:
        config = self._make_project_config()
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            confidence=0.5,
            tone_guard_applied=True,
        )
        assert should_auto_post(draft, config) is False

    def test_gate2_exactly_at_threshold(self) -> None:
        config = self._make_project_config()
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            confidence=0.85,
            tone_guard_applied=True,
        )
        assert should_auto_post(draft, config) is True

    def test_gate3_tone_guard_not_applied(self) -> None:
        config = self._make_project_config()
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            confidence=0.9,
            tone_guard_applied=False,
        )
        assert should_auto_post(draft, config) is False

    def test_default_posting_mode_is_draft_only(self) -> None:
        """Auto-posting is disabled by default."""
        config = ProjectConfig(owner="apache", repo="spark")
        assert config.posting.mode == "draft-only"
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            confidence=0.99,
            tone_guard_applied=True,
        )
        assert should_auto_post(draft, config) is False
