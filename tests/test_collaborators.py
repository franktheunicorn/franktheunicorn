"""Tests for collaborator detection."""

from __future__ import annotations

from franktheunicorn.scoring.collaborators import detect_collaborator, score_collaborator
from franktheunicorn.scoring.signals import WEIGHTS


class TestDetectCollaborator:
    def test_empty_history(self) -> None:
        assert detect_collaborator("alice", "holdenk", []) is False

    def test_below_threshold(self) -> None:
        history = [
            {"author": "alice", "reviewer": "holdenk"},
            {"author": "alice", "reviewer": "holdenk"},
        ]
        assert detect_collaborator("alice", "holdenk", history, threshold=3) is False

    def test_at_threshold(self) -> None:
        history = [
            {"author": "alice", "reviewer": "holdenk"},
            {"author": "alice", "reviewer": "holdenk"},
            {"author": "holdenk", "reviewer": "alice"},
        ]
        assert detect_collaborator("alice", "holdenk", history, threshold=3) is True

    def test_above_threshold(self) -> None:
        history = [
            {"author": "alice", "reviewer": "holdenk"},
            {"author": "holdenk", "reviewer": "alice"},
            {"author": "alice", "reviewer": "holdenk"},
            {"author": "holdenk", "reviewer": "alice"},
        ]
        assert detect_collaborator("alice", "holdenk", history, threshold=3) is True

    def test_unrelated_entries_ignored(self) -> None:
        history = [
            {"author": "bob", "reviewer": "carol"},
            {"author": "alice", "reviewer": "holdenk"},
            {"author": "bob", "reviewer": "holdenk"},
        ]
        assert detect_collaborator("alice", "holdenk", history, threshold=2) is False

    def test_case_insensitive(self) -> None:
        history = [
            {"author": "Alice", "reviewer": "HoldenK"},
            {"author": "alice", "reviewer": "holdenk"},
            {"author": "ALICE", "reviewer": "HOLDENK"},
        ]
        assert detect_collaborator("alice", "holdenk", history, threshold=3) is True

    def test_bidirectional_counting(self) -> None:
        """Both directions (operator reviews author, author reviews operator) count."""
        history = [
            {"author": "alice", "reviewer": "holdenk"},  # op reviewed alice
            {"author": "holdenk", "reviewer": "alice"},  # alice reviewed op
        ]
        assert detect_collaborator("alice", "holdenk", history, threshold=2) is True


class TestScoreCollaborator:
    def test_collaborator_scores(self) -> None:
        history = [
            {"author": "alice", "reviewer": "holdenk"},
            {"author": "alice", "reviewer": "holdenk"},
            {"author": "alice", "reviewer": "holdenk"},
        ]
        result = score_collaborator("alice", "holdenk", history)
        assert result == WEIGHTS["collaborator"]

    def test_non_collaborator(self) -> None:
        assert score_collaborator("alice", "holdenk", []) is None

    def test_custom_threshold(self) -> None:
        history = [{"author": "alice", "reviewer": "holdenk"}]
        assert (
            score_collaborator("alice", "holdenk", history, threshold=1) == WEIGHTS["collaborator"]
        )
