"""Fine-tuning evaluation metrics (v2 — §10.5).

Evaluates a fine-tuned model against a held-out eval set.
Checks: category accuracy, ROUGE-L, tone score, anti-pattern violations, FP rate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Evaluation thresholds (§10.5).
CATEGORY_ACCURACY_THRESHOLD = 0.80
ROUGE_L_THRESHOLD = 0.35
TONE_SCORE_THRESHOLD = 0.80
ANTI_PATTERN_VIOLATION_THRESHOLD = 0.05
FP_RATE_THRESHOLD = 0.20


@dataclass
class EvalResult:
    """Result of model evaluation."""

    passed: bool = False
    category_accuracy: float = 0.0
    rouge_l: float = 0.0
    tone_score: float = 0.0
    anti_pattern_violation_rate: float = 0.0
    fp_rate: float = 0.0
    total_examples: int = 0
    failures: list[str] = field(default_factory=list)
    error: str = ""


def _compute_rouge_l(reference: str, hypothesis: str) -> float:
    """Compute ROUGE-L F-score between reference and hypothesis.

    Uses a simple LCS-based implementation to avoid hard dependency on rouge-score.
    """
    if not reference or not hypothesis:
        return 0.0

    ref_tokens = reference.lower().split()
    hyp_tokens = hypothesis.lower().split()

    if not ref_tokens or not hyp_tokens:
        return 0.0

    # LCS via dynamic programming.
    m, n = len(ref_tokens), len(hyp_tokens)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if ref_tokens[i - 1] == hyp_tokens[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    lcs_len = dp[m][n]
    precision = lcs_len / n if n > 0 else 0.0
    recall = lcs_len / m if m > 0 else 0.0

    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _extract_category_from_text(text: str) -> str:
    """Extract the most likely review category from generated text."""
    categories = [
        "correctness",
        "style",
        "security",
        "test-coverage",
        "architectural",
        "naming",
        "suggested-change",
    ]
    lower = text.lower()
    for cat in categories:
        if cat in lower:
            return cat
    return "other"


def _check_anti_pattern_violation(text: str, anti_patterns: list[str]) -> bool:
    """Check if generated text matches any anti-pattern."""
    lower = text.lower()
    return any(pattern.lower() in lower for pattern in anti_patterns)


def evaluate_model(
    predictions: list[dict[str, Any]],
    eval_data: list[dict[str, Any]],
    *,
    anti_patterns: list[str] | None = None,
) -> EvalResult:
    """Evaluate fine-tuned model predictions against the eval set.

    ``predictions`` and ``eval_data`` must be aligned (same order, same length).
    Each entry should have "output" (or "chosen" for DPO) and optionally "category".
    """
    result = EvalResult()

    if not predictions or not eval_data:
        result.error = "Empty predictions or eval data"
        return result

    if len(predictions) != len(eval_data):
        result.error = f"Prediction count ({len(predictions)}) != eval count ({len(eval_data)})"
        return result

    result.total_examples = len(eval_data)
    anti_patterns = anti_patterns or []

    category_correct = 0
    rouge_scores: list[float] = []
    tone_passes = 0
    anti_pattern_violations = 0
    false_positives = 0

    for pred, gold in zip(predictions, eval_data, strict=True):
        pred_text = pred.get("output", pred.get("chosen", ""))
        gold_text = gold.get("output", gold.get("chosen", ""))

        # Category accuracy.
        pred_cat = pred.get("category") or _extract_category_from_text(pred_text)
        gold_cat = gold.get("category", _extract_category_from_text(gold_text))
        if pred_cat == gold_cat:
            category_correct += 1

        # ROUGE-L.
        rouge = _compute_rouge_l(gold_text, pred_text)
        rouge_scores.append(rouge)

        # Tone score — simple heuristic: no aggressive patterns.
        aggressive_patterns = ["you should know", "obviously", "clearly wrong", "terrible"]
        is_aggressive = any(p in pred_text.lower() for p in aggressive_patterns)
        if not is_aggressive:
            tone_passes += 1

        # Anti-pattern violations.
        if _check_anti_pattern_violation(pred_text, anti_patterns):
            anti_pattern_violations += 1

        # False positive: model generated a comment for a [REJECTED] example.
        if "[REJECTED]" in gold_text and pred_text.strip():
            false_positives += 1

    n = len(eval_data)
    result.category_accuracy = category_correct / n
    result.rouge_l = sum(rouge_scores) / n if rouge_scores else 0.0
    result.tone_score = tone_passes / n
    result.anti_pattern_violation_rate = anti_pattern_violations / n
    result.fp_rate = false_positives / n

    # Check thresholds.
    if result.category_accuracy < CATEGORY_ACCURACY_THRESHOLD:
        result.failures.append(
            f"Category accuracy {result.category_accuracy:.2f} < {CATEGORY_ACCURACY_THRESHOLD}"
        )
    if result.rouge_l < ROUGE_L_THRESHOLD:
        result.failures.append(f"ROUGE-L {result.rouge_l:.2f} < {ROUGE_L_THRESHOLD}")
    if result.tone_score < TONE_SCORE_THRESHOLD:
        result.failures.append(f"Tone score {result.tone_score:.2f} < {TONE_SCORE_THRESHOLD}")
    if result.anti_pattern_violation_rate > ANTI_PATTERN_VIOLATION_THRESHOLD:
        result.failures.append(
            f"Anti-pattern violations {result.anti_pattern_violation_rate:.2f} > "
            f"{ANTI_PATTERN_VIOLATION_THRESHOLD}"
        )
    if result.fp_rate > FP_RATE_THRESHOLD:
        result.failures.append(f"FP rate {result.fp_rate:.2f} > {FP_RATE_THRESHOLD}")

    result.passed = len(result.failures) == 0

    logger.info(
        "Evaluation: %s (cat=%.2f, rouge=%.2f, tone=%.2f, ap=%.2f, fp=%.2f)",
        "PASSED" if result.passed else "FAILED",
        result.category_accuracy,
        result.rouge_l,
        result.tone_score,
        result.anti_pattern_violation_rate,
        result.fp_rate,
    )

    return result


def save_eval_results(result: EvalResult, output_dir: Path) -> Path:
    """Save evaluation results to JSON."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "eval_results.json"
    data = {
        "passed": result.passed,
        "category_accuracy": result.category_accuracy,
        "rouge_l": result.rouge_l,
        "tone_score": result.tone_score,
        "anti_pattern_violation_rate": result.anti_pattern_violation_rate,
        "fp_rate": result.fp_rate,
        "total_examples": result.total_examples,
        "failures": result.failures,
    }
    path.write_text(json.dumps(data, indent=2))
    return path
