"""Tests for scored collaborator detection (§2.4)."""

from __future__ import annotations

from franktheunicorn.scoring.collaborators import compute_collaborator_score
from franktheunicorn.scoring.signals import WEIGHTS

W = WEIGHTS["collaborator"]


def _h(author: str, reviewer: str) -> dict[str, str]:
    return {"author": author, "reviewer": reviewer}


class TestComputeCollaboratorScore:
    def test_manual_entry_null_score(self) -> None:
        result = compute_collaborator_score("alice", "holdenk", [], [], {"alice": None})
        assert result == W

    def test_manual_entry_numeric(self) -> None:
        result = compute_collaborator_score("alice", "holdenk", [], [], {"alice": 50.0})
        assert result == round(W * 0.5)

    def test_manual_entry_full(self) -> None:
        result = compute_collaborator_score("alice", "holdenk", [], [], {"alice": 100.0})
        assert result == W

    def test_frequent_contributor(self) -> None:
        assert compute_collaborator_score("cloud-fan", "holdenk", [], ["cloud-fan"]) == W

    def test_frequent_contributor_case(self) -> None:
        assert compute_collaborator_score("Cloud-Fan", "holdenk", [], ["cloud-fan"]) == W

    def test_from_history(self) -> None:
        history = [_h("alice", "holdenk")] * 3
        result = compute_collaborator_score("alice", "holdenk", history, [])
        assert result == round((60 / 100) * W)

    def test_from_history_bidirectional(self) -> None:
        history = [_h("alice", "holdenk"), _h("holdenk", "alice")]
        result = compute_collaborator_score("alice", "holdenk", history, [])
        assert result == round((40 / 100) * W)

    def test_from_history_capped(self) -> None:
        history = [_h("alice", "holdenk")] * 10
        assert compute_collaborator_score("alice", "holdenk", history, []) == W

    def test_no_match(self) -> None:
        assert compute_collaborator_score("stranger", "holdenk", [], []) is None

    def test_priority_order(self) -> None:
        result = compute_collaborator_score("alice", "holdenk", [], ["alice"], {"alice": 50.0})
        assert result == round(W * 0.5)
