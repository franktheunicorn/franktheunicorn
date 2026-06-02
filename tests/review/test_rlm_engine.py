"""Tests for the RLM engine orchestration."""

from __future__ import annotations

import logging
from unittest.mock import patch

from franktheunicorn.config.models import RLMConfig
from franktheunicorn.review.backends.base import PRContext, ReviewFinding, ReviewResult
from franktheunicorn.review.rlm.engine import RLMEngine

_TWO_FILE_DIFF = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 x = 1
+y = 2
 z = 3
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1,2 +1,3 @@
 a = 1
+b = 2
 c = 3
"""


class RecordingLeaf:
    """A fake leaf backend that records its calls and returns per-file findings."""

    def __init__(self, calls: list[str], *, raise_exc: bool = False) -> None:
        self._calls = calls
        self._raise = raise_exc

    def generate_review(self, diff: str, pr_context: PRContext) -> ReviewResult:
        self._calls.append(diff)
        if self._raise:
            raise RuntimeError("leaf boom")
        files = [line[6:] for line in diff.split("\n") if line.startswith("+++ b/")]
        findings = [
            ReviewFinding(file_path=f, line_number=1, body=f"issue in {f}", severity="nit")
            for f in files
        ] or [ReviewFinding(file_path="x", line_number=1, body="generic", severity="nit")]
        return ReviewResult(overall_vibe=f"vibe:{len(files)}", findings=findings)


def _ctx() -> PRContext:
    return PRContext(
        pr_title="t",
        pr_body="b",
        pr_author="a",
        pr_number=1,
        project_name="o/r",
        review_context="",
        review_style="",
        tone="",
        test_expectations="",
        governance="standard",
    )


def _factory(calls: list[str], *, raise_exc: bool = False):
    return lambda: RecordingLeaf(calls, raise_exc=raise_exc)


def test_small_pr_fast_path_single_call() -> None:
    calls: list[str] = []
    config = RLMConfig(leaf_token_budget=100_000)
    engine = RLMEngine(config, _factory(calls))
    result = engine.review("+++ b/only.py\n", _ctx())
    assert len(calls) == 1
    assert any(f.file_path == "only.py" for f in result.findings)


def test_decomposition_one_call_per_file() -> None:
    calls: list[str] = []
    config = RLMConfig(leaf_token_budget=5, max_depth=1, max_sub_calls=30)
    engine = RLMEngine(config, _factory(calls))
    result = engine.review(_TWO_FILE_DIFF, _ctx())
    assert len(calls) == 2
    assert {f.file_path for f in result.findings} == {"a.py", "b.py"}


def test_max_sub_calls_cap_returns_partial(caplog) -> None:
    calls: list[str] = []
    config = RLMConfig(leaf_token_budget=5, max_depth=1, max_sub_calls=1)
    engine = RLMEngine(config, _factory(calls))
    with caplog.at_level(logging.WARNING):
        result = engine.review(_TWO_FILE_DIFF, _ctx())
    assert len(calls) == 1
    assert len(result.findings) == 1
    assert any("budget exhausted" in r.message for r in caplog.records)


def test_total_token_budget_cap_returns_partial() -> None:
    calls: list[str] = []
    config = RLMConfig(leaf_token_budget=5, max_depth=1, max_sub_calls=30, total_token_budget=1)
    engine = RLMEngine(config, _factory(calls))
    result = engine.review(_TWO_FILE_DIFF, _ctx())
    # Budget too small for even one leaf → no calls, no findings, no error.
    assert calls == []
    assert result.findings == []


def test_leaf_failure_is_swallowed() -> None:
    calls: list[str] = []
    config = RLMConfig(leaf_token_budget=5, max_depth=1)
    engine = RLMEngine(config, _factory(calls, raise_exc=True))
    result = engine.review(_TWO_FILE_DIFF, _ctx())
    assert len(calls) == 2  # both attempted
    assert result.findings == []  # both failed → empty, but no exception


def test_synthesis_call_sets_vibe() -> None:
    calls: list[str] = []
    config = RLMConfig(leaf_token_budget=5, max_depth=1, synthesis_call=True)
    engine = RLMEngine(config, _factory(calls))
    result = engine.review(_TWO_FILE_DIFF, _ctx())
    # 2 file leaves + 1 synthesis leaf.
    assert len(calls) == 3
    assert result.overall_vibe.startswith("vibe:")


class CostRecordingLeaf(RecordingLeaf):
    """A leaf that exposes record_cost like a real BaseLLMBackend."""

    def __init__(self, calls: list[str], cost_calls: list[tuple[int | None, int | None, str]]):
        super().__init__(calls)
        self._cost_calls = cost_calls

    def record_cost(
        self, project_id: int | None, pr_id: int | None, action_type: str = "review"
    ) -> None:
        self._cost_calls.append((project_id, pr_id, action_type))


def test_notebook_mode_returns_notebook_findings() -> None:
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.review.backends.base import ReviewFinding
    from franktheunicorn.review.rlm import sandbox_runner

    calls: list[str] = []
    config = RLMConfig(execution="notebook", leaf_token_budget=5)
    engine = RLMEngine(
        config,
        _factory(calls),
        model_configs={"stub": LLMBackendConfig(provider="stub")},
    )
    result_obj = sandbox_runner.RLMNotebookResult(
        findings=[ReviewFinding(file_path="x.py", body="nb finding")],
        overall_vibe="notebook vibe",
    )
    with patch.object(sandbox_runner, "run_rlm_notebook", return_value=result_obj):
        result = engine.review(_TWO_FILE_DIFF, _ctx())
    assert result.overall_vibe == "notebook vibe"
    assert result.findings[0].file_path == "x.py"
    assert calls == []  # map-reduce leaf path was NOT used


def test_notebook_mode_falls_back_to_map_reduce() -> None:
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.review.rlm import sandbox_runner

    calls: list[str] = []
    config = RLMConfig(execution="notebook", leaf_token_budget=5, max_depth=1)
    engine = RLMEngine(
        config,
        _factory(calls),
        model_configs={"stub": LLMBackendConfig(provider="stub")},
    )
    with patch.object(
        sandbox_runner,
        "run_rlm_notebook",
        side_effect=sandbox_runner.RLMSandboxUnavailableError("no docker"),
    ):
        result = engine.review(_TWO_FILE_DIFF, _ctx())
    assert len(calls) == 2  # fell back to map-reduce
    assert {f.file_path for f in result.findings} == {"a.py", "b.py"}


def test_notebook_mode_without_models_uses_map_reduce() -> None:
    calls: list[str] = []
    config = RLMConfig(execution="notebook", leaf_token_budget=5, max_depth=1)
    engine = RLMEngine(config, _factory(calls))  # no model_configs
    result = engine.review(_TWO_FILE_DIFF, _ctx())
    assert len(calls) == 2
    assert result.findings


def test_cost_recorded_per_leaf() -> None:
    calls: list[str] = []
    cost_calls: list[tuple[int | None, int | None, str]] = []
    config = RLMConfig(leaf_token_budget=5, max_depth=1)
    engine = RLMEngine(config, lambda: CostRecordingLeaf(calls, cost_calls))
    ctx = PRContext(
        pr_title="t",
        pr_body="b",
        pr_author="a",
        pr_number=1,
        project_name="o/r",
        review_context="",
        review_style="",
        tone="",
        test_expectations="",
        governance="standard",
        project_id=7,
        pr_id=99,
    )
    engine.review(_TWO_FILE_DIFF, ctx)
    assert len(cost_calls) == 2
    assert all(pid == 7 and prid == 99 and at == "rlm-leaf" for pid, prid, at in cost_calls)
