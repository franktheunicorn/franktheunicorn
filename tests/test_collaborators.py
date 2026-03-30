"""Tests for collaborator detection."""

from __future__ import annotations

from franktheunicorn.scoring.collaborators import detect_collaborator, score_collaborator
from franktheunicorn.scoring.signals import WEIGHTS


def _h(author: str, reviewer: str) -> dict[str, str]:
    """Shorthand for a review history entry."""
    return {"author": author, "reviewer": reviewer}


class TestDetectCollaborator:
    def test_empty(self) -> None:
        assert detect_collaborator("alice", "holdenk", []) is False

    def test_below_threshold(self) -> None:
        assert (
            detect_collaborator("alice", "holdenk", [_h("alice", "holdenk")] * 2, threshold=3)
            is False
        )

    def test_at_threshold(self) -> None:
        history = [_h("alice", "holdenk"), _h("alice", "holdenk"), _h("holdenk", "alice")]
        assert detect_collaborator("alice", "holdenk", history, threshold=3) is True

    def test_above_threshold(self) -> None:
        assert (
            detect_collaborator("alice", "holdenk", [_h("alice", "holdenk")] * 4, threshold=3)
            is True
        )

    def test_unrelated_ignored(self) -> None:
        history = [_h("bob", "carol"), _h("alice", "holdenk"), _h("bob", "holdenk")]
        assert detect_collaborator("alice", "holdenk", history, threshold=2) is False

    def test_case_insensitive(self) -> None:
        history = [_h("Alice", "HoldenK"), _h("alice", "holdenk"), _h("ALICE", "HOLDENK")]
        assert detect_collaborator("alice", "holdenk", history, threshold=3) is True

    def test_bidirectional(self) -> None:
        assert (
            detect_collaborator(
                "alice", "holdenk", [_h("alice", "holdenk"), _h("holdenk", "alice")], threshold=2
            )
            is True
        )


class TestScoreCollaborator:
    def test_collaborator(self) -> None:
        assert (
            score_collaborator("alice", "holdenk", [_h("alice", "holdenk")] * 3)
            == WEIGHTS["collaborator"]
        )

    def test_non_collaborator(self) -> None:
        assert score_collaborator("alice", "holdenk", []) is None

    def test_custom_threshold(self) -> None:
        assert (
            score_collaborator("alice", "holdenk", [_h("alice", "holdenk")], threshold=1)
            == WEIGHTS["collaborator"]
        )
