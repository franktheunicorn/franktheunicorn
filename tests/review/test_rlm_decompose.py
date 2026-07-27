"""Tests for RLM diff decomposition."""

from __future__ import annotations

from franktheunicorn.review.backends.base import PRContext
from franktheunicorn.review.rlm.decompose import (
    fits_single_call,
    partition,
)

_TWO_FILE_DIFF = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,3 +1,4 @@
 import os
+import sys
 x = 1
 y = 2
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1,2 +1,3 @@
 a = 1
+b = 2
 c = 3
"""

_TWO_HUNK_DIFF = """\
diff --git a/big.py b/big.py
--- a/big.py
+++ b/big.py
@@ -1,2 +1,3 @@
 line1
+added1
 line2
@@ -20,2 +21,3 @@
 line20
+added20
 line21
"""


def _ctx(**kwargs: object) -> PRContext:
    defaults: dict[str, object] = {
        "pr_title": "t",
        "pr_body": "b",
        "pr_author": "a",
        "pr_number": 1,
        "project_name": "o/r",
        "review_context": "",
        "review_style": "",
        "tone": "",
        "test_expectations": "",
        "governance": "standard",
    }
    defaults.update(kwargs)
    return PRContext(**defaults)  # type: ignore[arg-type]


def test_partition_one_node_per_file() -> None:
    nodes = partition(_TWO_FILE_DIFF, _ctx(), leaf_token_budget=100_000, max_depth=2)
    assert {n.file_path for n in nodes} == {"a.py", "b.py"}


def test_partition_splits_oversized_file_into_hunks() -> None:
    # A tiny budget forces depth-2 hunk splitting for the multi-hunk file.
    nodes = partition(_TWO_HUNK_DIFF, _ctx(), leaf_token_budget=1, max_depth=2)
    assert len(nodes) == 2
    assert all(n.file_path == "big.py" for n in nodes)
    # Each hunk node re-heads itself so leaves still know the file.
    assert all("+++ b/big.py" in n.diff for n in nodes)


def test_partition_keeps_file_whole_at_max_depth_one() -> None:
    nodes = partition(_TWO_HUNK_DIFF, _ctx(), leaf_token_budget=1, max_depth=1)
    assert len(nodes) == 1
    assert nodes[0].file_path == "big.py"


def test_partition_unparseable_diff_falls_back_to_single_node() -> None:
    nodes = partition("not a diff at all", _ctx(), leaf_token_budget=10, max_depth=2)
    assert len(nodes) == 1
    assert nodes[0].file_path == ""


def test_partition_empty_diff_falls_back_to_single_node() -> None:
    nodes = partition("", _ctx(), leaf_token_budget=10, max_depth=2)
    assert len(nodes) == 1


def test_full_file_context_is_scoped_per_file() -> None:
    full = (
        "## Full file context\n\n"
        "### a.py\n```python\nA_ONLY_MARKER = 1\n```\n\n"
        "### b.py\n```python\nB_ONLY_MARKER = 2\n```"
    )
    nodes = partition(
        _TWO_FILE_DIFF, _ctx(full_file_context=full), leaf_token_budget=100_000, max_depth=2
    )
    by_path = {n.file_path: n for n in nodes}
    assert "A_ONLY_MARKER" in by_path["a.py"].pr_context.full_file_context
    assert "B_ONLY_MARKER" not in by_path["a.py"].pr_context.full_file_context
    assert "B_ONLY_MARKER" in by_path["b.py"].pr_context.full_file_context


def test_fits_single_call() -> None:
    assert fits_single_call("+++ b/x.py\n", _ctx(), leaf_token_budget=1000) is True
    assert fits_single_call("x" * 10_000, _ctx(), leaf_token_budget=5) is False
