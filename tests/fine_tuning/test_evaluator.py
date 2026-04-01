"""Tests for fine-tuning evaluation metrics (v2 — §10.5)."""

from __future__ import annotations

from pathlib import Path

from franktheunicorn.fine_tuning.evaluator import (
    ANTI_PATTERN_VIOLATION_THRESHOLD,
    CATEGORY_ACCURACY_THRESHOLD,
    FP_RATE_THRESHOLD,
    ROUGE_L_THRESHOLD,
    TONE_SCORE_THRESHOLD,
    EvalResult,
    evaluate_model,
    save_eval_results,
)


def _make_predictions(n: int, *, good: bool = True) -> list[dict[str, str]]:
    """Generate aligned predictions and eval data."""
    preds = []
    evals = []
    for i in range(n):
        if good:
            text = f"Consider using correctness check {i}. Great approach here."
            preds.append({"output": text, "category": "correctness"})
            evals.append({"output": text, "category": "correctness"})
        else:
            preds.append({"output": f"You should know this is clearly wrong {i}"})
            evals.append({"output": f"Consider fixing issue {i}", "category": "style"})
    return preds, evals  # type: ignore[return-value]


class TestEvaluateModel:
    def test_perfect_predictions(self) -> None:
        preds, evals = _make_predictions(20)
        result = evaluate_model(preds, evals)

        assert result.passed is True
        assert result.category_accuracy == 1.0
        assert result.rouge_l == 1.0
        assert result.tone_score == 1.0
        assert result.anti_pattern_violation_rate == 0.0
        assert result.fp_rate == 0.0

    def test_poor_predictions_fail(self) -> None:
        preds, evals = _make_predictions(20, good=False)
        result = evaluate_model(preds, evals)

        assert result.passed is False
        assert len(result.failures) > 0

    def test_empty_inputs(self) -> None:
        result = evaluate_model([], [])
        assert result.error != ""

    def test_mismatched_lengths(self) -> None:
        result = evaluate_model(
            [{"output": "a"}],
            [{"output": "b"}, {"output": "c"}],
        )
        assert result.error != ""
        assert "count" in result.error.lower()

    def test_anti_pattern_detection(self) -> None:
        preds = [{"output": "Don't use print statements in production code"}]
        evals = [{"output": "Avoid print statements", "category": "style"}]

        result = evaluate_model(preds, evals, anti_patterns=["print statements"])
        assert result.anti_pattern_violation_rate == 1.0

    def test_no_anti_pattern_violation(self) -> None:
        preds = [{"output": "Consider using logging instead"}]
        evals = [{"output": "Use logging", "category": "style"}]

        result = evaluate_model(preds, evals, anti_patterns=["print statements"])
        assert result.anti_pattern_violation_rate == 0.0

    def test_tone_detects_aggressive(self) -> None:
        preds = [
            {"output": "You should know that this is obviously wrong"},
            {"output": "Consider using a different approach here"},
        ]
        evals = [
            {"output": "Use a different approach", "category": "correctness"},
            {"output": "Consider using a different approach", "category": "correctness"},
        ]

        result = evaluate_model(preds, evals)
        assert result.tone_score == 0.5  # 1 of 2 passes

    def test_fp_rate_on_rejected(self) -> None:
        preds = [
            {"output": "This looks problematic"},
            {"output": ""},
        ]
        evals = [
            {"output": "[REJECTED] Too pedantic", "category": "other"},
            {"output": "[REJECTED] Not useful", "category": "other"},
        ]

        result = evaluate_model(preds, evals)
        assert result.fp_rate == 0.5  # model generated for 1 of 2 rejects

    def test_threshold_values(self) -> None:
        assert CATEGORY_ACCURACY_THRESHOLD == 0.80
        assert ROUGE_L_THRESHOLD == 0.35
        assert TONE_SCORE_THRESHOLD == 0.80
        assert ANTI_PATTERN_VIOLATION_THRESHOLD == 0.05
        assert FP_RATE_THRESHOLD == 0.20


class TestSaveEvalResults:
    def test_saves_json(self, tmp_path: Path) -> None:
        result = EvalResult(
            passed=True,
            category_accuracy=0.85,
            rouge_l=0.42,
            tone_score=0.90,
            total_examples=50,
        )
        path = save_eval_results(result, tmp_path / "output")

        assert path.exists()
        import json

        data = json.loads(path.read_text())
        assert data["passed"] is True
        assert data["category_accuracy"] == 0.85
        assert data["rouge_l"] == 0.42

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "nested" / "dir"
        result = EvalResult(passed=False, failures=["cat too low"])
        path = save_eval_results(result, output_dir)

        assert path.exists()
        import json

        data = json.loads(path.read_text())
        assert data["passed"] is False
        assert "cat too low" in data["failures"]
