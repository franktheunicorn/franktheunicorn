"""Tests for blame-based proximity scoring."""

from __future__ import annotations

from franktheunicorn.scoring.blame import score_blame_proximity
from franktheunicorn.scoring.signals import WEIGHTS

W = WEIGHTS["blame_proximity"]


class TestScoreBlameProximity:
    def test_empty(self) -> None:
        assert score_blame_proximity([], "holdenk") is None

    def test_all_files(self) -> None:
        blame = [
            {"file_path": "a.py", "authors": ["holdenk", "alice"]},
            {"file_path": "b.py", "authors": ["holdenk"]},
        ]
        assert score_blame_proximity(blame, "holdenk") == round(W, 4)

    def test_some_files(self) -> None:
        blame = [
            {"file_path": "a.py", "authors": ["holdenk"]},
            {"file_path": "b.py", "authors": ["alice"]},
        ]
        assert score_blame_proximity(blame, "holdenk") == round(W * 0.5, 4)

    def test_no_files(self) -> None:
        blame = [{"file_path": "a.py", "authors": ["alice"]}]
        assert score_blame_proximity(blame, "holdenk") is None

    def test_case_insensitive(self) -> None:
        assert (
            score_blame_proximity([{"file_path": "a.py", "authors": ["HoldenK"]}], "holdenk")
            is not None
        )

    def test_empty_authors(self) -> None:
        assert score_blame_proximity([{"file_path": "a.py", "authors": []}], "holdenk") is None
