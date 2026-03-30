"""Tests for blame-based proximity scoring."""

from __future__ import annotations

from franktheunicorn.scoring.blame import score_blame_proximity
from franktheunicorn.scoring.signals import WEIGHTS


class TestScoreBlameProximity:
    def test_empty_blame_data(self) -> None:
        assert score_blame_proximity([], "holdenk") is None

    def test_operator_in_all_files(self) -> None:
        blame = [
            {"file_path": "a.py", "authors": ["holdenk", "alice"]},
            {"file_path": "b.py", "authors": ["holdenk"]},
        ]
        result = score_blame_proximity(blame, "holdenk")
        assert result == round(WEIGHTS["blame_proximity"] * 1.0, 4)

    def test_operator_in_some_files(self) -> None:
        blame = [
            {"file_path": "a.py", "authors": ["holdenk"]},
            {"file_path": "b.py", "authors": ["alice"]},
        ]
        result = score_blame_proximity(blame, "holdenk")
        assert result == round(WEIGHTS["blame_proximity"] * 0.5, 4)

    def test_operator_in_no_files(self) -> None:
        blame = [
            {"file_path": "a.py", "authors": ["alice"]},
            {"file_path": "b.py", "authors": ["bob"]},
        ]
        assert score_blame_proximity(blame, "holdenk") is None

    def test_case_insensitive(self) -> None:
        blame = [{"file_path": "a.py", "authors": ["HoldenK"]}]
        result = score_blame_proximity(blame, "holdenk")
        assert result is not None
        assert result > 0

    def test_empty_authors_list(self) -> None:
        blame = [{"file_path": "a.py", "authors": []}]
        assert score_blame_proximity(blame, "holdenk") is None
