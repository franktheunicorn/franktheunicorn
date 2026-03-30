"""Tests for custom scoring expression sandbox."""

from __future__ import annotations

from franktheunicorn.scoring.sandbox import evaluate_custom_score


class TestEvaluateCustomScore:
    def test_simple(self) -> None:
        assert evaluate_custom_score("0.5", pr={"author": "alice"}, config={}) == 0.5

    def test_pr_access(self) -> None:
        assert (
            evaluate_custom_score(
                "0.1 if pr['author'] == 'alice' else 0.0", pr={"author": "alice"}, config={}
            )
            == 0.1
        )

    def test_config_access(self) -> None:
        assert (
            evaluate_custom_score(
                "0.2 if len(config.get('watched_paths', [])) > 0 else 0.0",
                pr={},
                config={"watched_paths": ["src/"]},
            )
            == 0.2
        )

    def test_builtins(self) -> None:
        result = evaluate_custom_score(
            "min(len(pr.get('changed_files', [])) / 10, 1.0) * 0.1",
            pr={"changed_files": ["a.py", "b.py", "c.py"]},
            config={},
        )
        assert result is not None and abs(result - 0.03) < 1e-4

    def test_clamped(self) -> None:
        assert evaluate_custom_score("5.0", pr={}, config={}) == 1.0
        assert evaluate_custom_score("-5.0", pr={}, config={}) == -1.0

    def test_empty(self) -> None:
        assert evaluate_custom_score("", pr={}, config={}) is None
        assert evaluate_custom_score("   ", pr={}, config={}) is None

    def test_errors_return_none(self) -> None:
        assert evaluate_custom_score("if True:", pr={}, config={}) is None  # syntax
        assert evaluate_custom_score("1 / 0", pr={}, config={}) is None  # runtime
        assert evaluate_custom_score("unknown_var", pr={}, config={}) is None  # name

    def test_security_rejected(self) -> None:
        assert (
            evaluate_custom_score("__import__('os').system('echo pwned')", pr={}, config={})
            is None
        )
        assert evaluate_custom_score("pr.__class__.__bases__", pr={}, config={}) is None

    def test_non_numeric(self) -> None:
        assert evaluate_custom_score("'hello'", pr={}, config={}) is None
        assert evaluate_custom_score("None", pr={}, config={}) is None

    def test_bool_coerced(self) -> None:
        assert evaluate_custom_score("True", pr={}, config={}) == 1.0

    def test_negative(self) -> None:
        assert evaluate_custom_score("-0.05", pr={}, config={}) == -0.05

    def test_conditional(self) -> None:
        assert (
            evaluate_custom_score(
                "-0.1 if pr.get('additions', 0) > 1000 else 0.0",
                pr={"additions": 2000},
                config={},
            )
            == -0.1
        )
