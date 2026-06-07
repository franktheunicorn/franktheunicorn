"""Decompose a PR into bounded work units for the RLM engine.

The recursion is data-driven, not model-driven: a PR is split per changed
file, and a file is split further into hunks only when its diff plus its
scoped context would exceed the per-leaf token budget. Each resulting
``RLMNode`` carries a *scoped* ``PRContext`` so leaf reviews see only the
full-file context for their own file — re-stuffing the whole context into
every leaf would defeat the purpose of decomposing.
"""

from __future__ import annotations

import dataclasses
import logging

from franktheunicorn.review.backends.base import PRContext
from franktheunicorn.review.rlm.budget import estimate_tokens

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class RLMNode:
    """One unit of review work: a slice of diff plus its scoped context."""

    file_path: str
    diff: str
    pr_context: PRContext
    est_tokens: int


def _slice_full_file_context(full_ctx: str, file_path: str) -> str:
    """Return only the ``### <file_path>`` block of a full-file context string.

    ``context_builder._render`` formats each file as a ``### {rel}`` heading
    followed by a fenced code block. We keep the block whose heading matches
    ``file_path`` and drop the rest. Returns "" when there's no match.
    """
    if not full_ctx or not file_path:
        return ""

    target = f"### {file_path}"
    captured: list[str] = []
    capturing = False
    for line in full_ctx.split("\n"):
        if line.startswith("### "):
            capturing = line.strip() == target
        if capturing:
            captured.append(line)

    if not captured:
        return ""
    return "## Full file context\n\n" + "\n".join(captured).rstrip()


def _scope_context(pr_context: PRContext, file_path: str) -> PRContext:
    """Copy ``pr_context`` with full-file context narrowed to ``file_path``."""
    return dataclasses.replace(
        pr_context,
        full_file_context=_slice_full_file_context(pr_context.full_file_context, file_path),
    )


def _whole_node(diff: str, pr_context: PRContext) -> RLMNode:
    """Fallback single node covering the entire diff (no decomposition)."""
    est = estimate_tokens(diff) + estimate_tokens(pr_context.full_file_context)
    return RLMNode(file_path="", diff=diff, pr_context=pr_context, est_tokens=est)


def _file_header(path: str) -> str:
    """Synthetic unified-diff header so hunk-only slices still name their file."""
    return f"--- a/{path}\n+++ b/{path}\n"


def partition(
    diff: str,
    pr_context: PRContext,
    *,
    leaf_token_budget: int,
    max_depth: int,
) -> list[RLMNode]:
    """Split ``diff`` into ``RLMNode``s bounded by ``leaf_token_budget``.

    Depth 1 is per-file; depth 2 splits an oversized file into per-hunk nodes.
    ``max_depth`` of 1 keeps everything at file granularity. On any parse
    failure (or an empty diff) returns a single whole-diff node so the caller
    still gets a review.
    """
    try:
        from unidiff import PatchSet  # type: ignore[import-untyped]

        patch = PatchSet(diff)
    except Exception:
        logger.debug("RLM: could not parse diff; reviewing as a single unit.", exc_info=True)
        return [_whole_node(diff, pr_context)]

    patched_files = list(patch)
    if not patched_files:
        return [_whole_node(diff, pr_context)]

    nodes: list[RLMNode] = []
    for patched_file in patched_files:
        file_path = patched_file.path
        file_diff = str(patched_file)
        scoped = _scope_context(pr_context, file_path)
        scoped_tokens = estimate_tokens(scoped.full_file_context)
        file_est = estimate_tokens(file_diff) + scoped_tokens

        # Keep the file whole when it fits, or when hunk-level recursion is off.
        if file_est <= leaf_token_budget or max_depth < 2:
            nodes.append(
                RLMNode(
                    file_path=file_path,
                    diff=file_diff,
                    pr_context=scoped,
                    est_tokens=file_est,
                )
            )
            continue

        # Recurse: one node per hunk, each re-headed so it names its file.
        header = _file_header(file_path)
        hunk_nodes = [
            RLMNode(
                file_path=file_path,
                diff=header + str(hunk),
                pr_context=scoped,
                est_tokens=estimate_tokens(str(hunk)) + scoped_tokens,
            )
            for hunk in patched_file
        ]
        nodes.extend(
            hunk_nodes
            or [
                RLMNode(
                    file_path=file_path,
                    diff=file_diff,
                    pr_context=scoped,
                    est_tokens=file_est,
                )
            ]
        )

    return nodes


def fits_single_call(diff: str, pr_context: PRContext, leaf_token_budget: int) -> bool:
    """True when the whole PR fits one leaf — skip decomposition entirely."""
    est = estimate_tokens(diff) + estimate_tokens(pr_context.full_file_context)
    return est <= leaf_token_budget
