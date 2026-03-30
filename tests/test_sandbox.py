"""Tests for custom scoring expression sandbox."""

from __future__ import annotations

from franktheunicorn.scoring.sandbox import evaluate_custom_score


class TestEvaluateCustomScore:
    def test_simple_expression(self) -> None:
        result = evaluate_custom_score(
            "0.5",
            pr={"author": "alice"},
            config={},
        )
        assert result == 0.5

    def test_pr_data_access(self) -> None:
        result = evaluate_custom_score(
            "0.1 if pr['author'] == 'alice' else 0.0",
            pr={"author": "alice"},
            config={},
        )
        assert result == 0.1

    def test_config_data_access(self) -> None:
        result = evaluate_custom_score(
            "0.2 if len(config.get('watched_paths', [])) > 0 else 0.0",
            pr={},
            config={"watched_paths": ["src/"]},
        )
        assert result == 0.2

    def test_builtin_functions(self) -> None:
        result = evaluate_custom_score(
            "min(len(pr.get('changed_files', [])) / 10, 1.0) * 0.1",
            pr={"changed_files": ["a.py", "b.py", "c.py"]},
            config={},
        )
        assert result is not None
        assert abs(result - 0.03) < 1e-4

    def test_clamped_high(self) -> None:
        result = evaluate_custom_score("5.0", pr={}, config={})
        assert result == 1.0

    def test_clamped_low(self) -> None:
        result = evaluate_custom_score("-5.0", pr={}, config={})
        assert result == -1.0

    def test_empty_expression(self) -> None:
        assert evaluate_custom_score("", pr={}, config={}) is None

    def test_whitespace_expression(self) -> None:
        assert evaluate_custom_score("   ", pr={}, config={}) is None

    def test_syntax_error(self) -> None:
        assert evaluate_custom_score("if True:", pr={}, config={}) is None

    def test_runtime_error(self) -> None:
        assert evaluate_custom_score("1 / 0", pr={}, config={}) is None

    def test_undefined_variable(self) -> None:
        assert evaluate_custom_score("unknown_var", pr={}, config={}) is None

    def test_import_rejected(self) -> None:
        assert (
            evaluate_custom_score(
                "__import__('os').system('echo pwned')",
                pr={},
                config={},
            )
            is None
        )

    def test_dunder_access_rejected(self) -> None:
        assert (
            evaluate_custom_score(
                "pr.__class__.__bases__",
                pr={},
                config={},
            )
            is None
        )

    def test_non_numeric_result(self) -> None:
        assert evaluate_custom_score("'hello'", pr={}, config={}) is None

    def test_none_result(self) -> None:
        assert evaluate_custom_score("None", pr={}, config={}) is None

    def test_bool_coerced_to_float(self) -> None:
        result = evaluate_custom_score("True", pr={}, config={})
        assert result == 1.0

    def test_negative_score(self) -> None:
        result = evaluate_custom_score("-0.05", pr={}, config={})
        assert result == -0.05

    def test_conditional_on_additions(self) -> None:
        result = evaluate_custom_score(
            "-0.1 if pr.get('additions', 0) > 1000 else 0.0",
            pr={"additions": 2000},
            config={},
        )
        assert result == -0.1
