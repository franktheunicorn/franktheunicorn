"""Tests for blame-based scoring (§2.5)."""

from __future__ import annotations

from franktheunicorn.scoring.blame import score_touches_operator_code
from franktheunicorn.scoring.signals import WEIGHTS

W = WEIGHTS["touches_operator_code"]


class TestScoreTouchesOperatorCode:
    def test_empty(self) -> None:
        assert score_touches_operator_code([], "holdenk") is None

    def test_layer1_all_files(self) -> None:
        blame = [
            {"file_path": "a.py", "authors": ["holdenk", "alice"]},
            {"file_path": "b.py", "authors": ["holdenk"]},
        ]
        assert score_touches_operator_code(blame, "holdenk") == W

    def test_layer1_some_files(self) -> None:
        blame = [
            {"file_path": "a.py", "authors": ["holdenk"]},
            {"file_path": "b.py", "authors": ["alice"]},
        ]
        assert score_touches_operator_code(blame, "holdenk") == round(W * 0.5)

    def test_layer2_proximity(self) -> None:
        blame = [
            {"file_path": "a.py", "authors": ["alice"], "near_authors": ["holdenk"]},
            {"file_path": "b.py", "authors": ["bob"], "near_authors": []},
        ]
        assert score_touches_operator_code(blame, "holdenk") == round(W * 0.25)

    def test_layer1_overrides_layer2(self) -> None:
        blame = [{"file_path": "a.py", "authors": ["holdenk"], "near_authors": ["holdenk"]}]
        assert score_touches_operator_code(blame, "holdenk") == W

    def test_no_overlap(self) -> None:
        blame = [{"file_path": "a.py", "authors": ["alice"]}]
        assert score_touches_operator_code(blame, "holdenk") is None

    def test_case_insensitive(self) -> None:
        blame = [{"file_path": "a.py", "authors": ["HoldenK"]}]
        assert score_touches_operator_code(blame, "holdenk") is not None
