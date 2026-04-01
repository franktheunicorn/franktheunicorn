"""Tests for confidence-gated auto-posting (v1.5 triple gate)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from franktheunicorn.config.models import PostingConfig, ProjectConfig
from franktheunicorn.review.auto_poster import auto_post_findings, should_auto_post
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

    def test_gate2_anti_pattern_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gate 2 rejects when anti-pattern matches are found."""
        config = self._make_project_config()
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            confidence=0.95,
            tone_guard_applied=True,
        )
        monkeypatch.setattr(
            "franktheunicorn.review.auto_poster.check_against_anti_patterns",
            lambda body, project: ["fake-anti-pattern"],
        )
        assert should_auto_post(draft, config) is False


@pytest.mark.django_db
class TestAutoPostFindings:
    """Tests for the auto_post_findings function."""

    def _make_project_config(
        self,
        mode: str = "confidence-gated",
        bot_token_env: str = "GITHUB_TOKEN_BOT",
    ) -> ProjectConfig:
        return ProjectConfig(
            owner="apache",
            repo="spark",
            posting=PostingConfig(
                mode=mode,
                confidence_threshold=0.85,
                bot_token_env=bot_token_env,
            ),
        )

    def test_returns_empty_when_draft_only(self) -> None:
        """Early exit when posting mode is draft-only."""
        config = self._make_project_config(mode="draft-only")
        pr = PullRequestFactory()
        result = auto_post_findings(pr, config)
        assert result == []

    def test_filters_pending_drafts_through_triple_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only drafts passing all three gates are included in eligible list."""
        config = self._make_project_config()
        pr = PullRequestFactory()
        # Eligible: high confidence + tone guard applied
        eligible_draft = ReviewDraftFactory(
            pull_request=pr,
            confidence=0.95,
            tone_guard_applied=True,
            status="pending",
        )
        # Not eligible: low confidence
        ReviewDraftFactory(
            pull_request=pr,
            confidence=0.3,
            tone_guard_applied=True,
            status="pending",
        )
        # Not eligible: tone guard not applied
        ReviewDraftFactory(
            pull_request=pr,
            confidence=0.95,
            tone_guard_applied=False,
            status="pending",
        )
        # Not eligible: already approved (not pending)
        ReviewDraftFactory(
            pull_request=pr,
            confidence=0.95,
            tone_guard_applied=True,
            status="approved",
        )

        # Ensure anti-pattern check returns no matches
        monkeypatch.setattr(
            "franktheunicorn.review.auto_poster.check_against_anti_patterns",
            lambda body, project: [],
        )
        monkeypatch.setenv("GITHUB_TOKEN_BOT", "ghp_faketoken123")

        # Mock GitHubClient and GitHubPoster
        mock_client = MagicMock()
        mock_poster = MagicMock()
        mock_poster.post_review.return_value = {"id": 1}

        monkeypatch.setattr(
            "franktheunicorn.github.client.GitHubClient",
            lambda token: mock_client,
        )
        monkeypatch.setattr(
            "franktheunicorn.github.poster.GitHubPoster",
            lambda client, attribution: mock_poster,
        )

        result = auto_post_findings(pr, config)
        assert len(result) == 1
        assert result[0].pk == eligible_draft.pk
        mock_poster.post_review.assert_called_once()

    def test_returns_empty_when_no_eligible_drafts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns empty list when no drafts pass the triple gate."""
        config = self._make_project_config()
        pr = PullRequestFactory()
        # Only low-confidence drafts
        ReviewDraftFactory(
            pull_request=pr,
            confidence=0.3,
            tone_guard_applied=True,
            status="pending",
        )

        monkeypatch.setattr(
            "franktheunicorn.review.auto_poster.check_against_anti_patterns",
            lambda body, project: [],
        )

        result = auto_post_findings(pr, config)
        assert result == []

    def test_skips_when_bot_token_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns empty when the bot token env var is not set."""
        config = self._make_project_config()
        pr = PullRequestFactory()
        ReviewDraftFactory(
            pull_request=pr,
            confidence=0.95,
            tone_guard_applied=True,
            status="pending",
        )

        monkeypatch.setattr(
            "franktheunicorn.review.auto_poster.check_against_anti_patterns",
            lambda body, project: [],
        )
        monkeypatch.delenv("GITHUB_TOKEN_BOT", raising=False)

        result = auto_post_findings(pr, config)
        assert result == []

    def test_posts_via_github_poster(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Successfully posts via GitHubPoster and returns eligible drafts."""
        config = self._make_project_config()
        pr = PullRequestFactory()
        draft = ReviewDraftFactory(
            pull_request=pr,
            confidence=0.92,
            tone_guard_applied=True,
            status="pending",
        )

        monkeypatch.setattr(
            "franktheunicorn.review.auto_poster.check_against_anti_patterns",
            lambda body, project: [],
        )
        monkeypatch.setenv("GITHUB_TOKEN_BOT", "ghp_faketoken123")

        mock_client = MagicMock()
        mock_poster = MagicMock()
        mock_poster.post_review.return_value = {"id": 42}

        monkeypatch.setattr(
            "franktheunicorn.github.client.GitHubClient",
            lambda token: mock_client,
        )
        monkeypatch.setattr(
            "franktheunicorn.github.poster.GitHubPoster",
            lambda client, attribution: mock_poster,
        )

        result = auto_post_findings(pr, config)
        assert len(result) == 1
        assert result[0].pk == draft.pk
        # Verify poster was called with PR and the eligible drafts
        call_args = mock_poster.post_review.call_args
        assert call_args[0][0] == pr
        assert len(call_args[0][1]) == 1
        # Verify client was closed
        mock_client.close.assert_called_once()

    def test_handles_posting_exception_gracefully(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns empty list when posting raises an exception."""
        config = self._make_project_config()
        pr = PullRequestFactory()
        ReviewDraftFactory(
            pull_request=pr,
            confidence=0.95,
            tone_guard_applied=True,
            status="pending",
        )

        monkeypatch.setattr(
            "franktheunicorn.review.auto_poster.check_against_anti_patterns",
            lambda body, project: [],
        )
        monkeypatch.setenv("GITHUB_TOKEN_BOT", "ghp_faketoken123")

        def raise_import_error(token: str) -> None:
            msg = "Simulated connection error"
            raise ConnectionError(msg)

        monkeypatch.setattr(
            "franktheunicorn.github.client.GitHubClient",
            raise_import_error,
        )

        result = auto_post_findings(pr, config)
        assert result == []

    def test_client_closed_even_on_poster_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GitHubClient.close() is called even when poster.post_review raises."""
        config = self._make_project_config()
        pr = PullRequestFactory()
        ReviewDraftFactory(
            pull_request=pr,
            confidence=0.95,
            tone_guard_applied=True,
            status="pending",
        )

        monkeypatch.setattr(
            "franktheunicorn.review.auto_poster.check_against_anti_patterns",
            lambda body, project: [],
        )
        monkeypatch.setenv("GITHUB_TOKEN_BOT", "ghp_faketoken123")

        mock_client = MagicMock()
        mock_poster = MagicMock()
        mock_poster.post_review.side_effect = RuntimeError("API failure")

        monkeypatch.setattr(
            "franktheunicorn.github.client.GitHubClient",
            lambda token: mock_client,
        )
        monkeypatch.setattr(
            "franktheunicorn.github.poster.GitHubPoster",
            lambda client, attribution: mock_poster,
        )

        result = auto_post_findings(pr, config)
        # Exception is caught, so empty list returned
        assert result == []
        # But client.close() was still called (finally block)
        mock_client.close.assert_called_once()
