"""The Recursive Language Model orchestrator.

``RLMEngine`` turns one big PR review into many small, focused leaf reviews:
it decomposes the diff (:mod:`decompose`), dispatches each unit to a fresh
leaf backend under a shared :class:`RLMBudget`, and reduces the results
(:mod:`aggregate`). A fresh leaf instance per call keeps per-call cost
tracking race-free when leaves run concurrently.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import ReviewResult
from franktheunicorn.review.rlm.aggregate import aggregate_review
from franktheunicorn.review.rlm.budget import RLMBudget, estimate_tokens
from franktheunicorn.review.rlm.decompose import RLMNode, fits_single_call, partition

if TYPE_CHECKING:
    from franktheunicorn.config.models import RLMConfig
    from franktheunicorn.review.backends.base import LLMBackend, PRContext

logger = logging.getLogger(__name__)

LeafFactory = Callable[[], "LLMBackend"]


class RLMEngine:
    """Recursive review orchestrator over an injected leaf backend factory."""

    def __init__(self, config: RLMConfig, leaf_factory: LeafFactory) -> None:
        self._config = config
        self._leaf_factory = leaf_factory

    def review(self, diff: str, pr_context: PRContext) -> ReviewResult:
        """Recursively review ``diff`` and return one aggregated result."""
        leaf_budget = self._config.leaf_token_budget

        # Small-PR fast path: behave exactly like a single normal backend call.
        if fits_single_call(diff, pr_context, leaf_budget):
            return self._run_leaf(diff, pr_context, "rlm-leaf")

        nodes = partition(
            diff,
            pr_context,
            leaf_token_budget=leaf_budget,
            max_depth=self._config.max_depth,
        )
        budget = RLMBudget(
            max_sub_calls=self._config.max_sub_calls,
            total_token_budget=self._config.total_token_budget,
        )
        results = self._dispatch(nodes, budget)

        synthesized_vibe = ""
        if self._config.synthesis_call:
            synth_diff = diff[: leaf_budget * 4]
            if budget.can_afford(estimate_tokens(synth_diff)):
                budget.charge(estimate_tokens(synth_diff))
                synthesized_vibe = self._run_leaf(
                    synth_diff, pr_context, "rlm-synthesis"
                ).overall_vibe

        return aggregate_review(results, synthesized_vibe=synthesized_vibe)

    def _dispatch(self, nodes: list[RLMNode], budget: RLMBudget) -> list[ReviewResult]:
        """Reserve budget sequentially, then run affordable leaves in parallel."""
        affordable: list[RLMNode] = []
        for node in nodes:
            if budget.can_afford(node.est_tokens):
                budget.charge(node.est_tokens)
                affordable.append(node)

        if not affordable:
            return []

        workers = max(1, min(self._config.concurrency, len(affordable)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(self._run_leaf, node.diff, node.pr_context, "rlm-leaf")
                for node in affordable
            ]
            return [future.result() for future in futures]

    def _run_leaf(self, diff: str, pr_context: PRContext, action_type: str) -> ReviewResult:
        """Run one leaf review on a fresh backend and record its cost."""
        backend = self._leaf_factory()
        try:
            result = backend.generate_review(diff, pr_context)
        except Exception:
            logger.debug("RLM leaf review failed; skipping this unit.", exc_info=True)
            return ReviewResult()
        self._record_cost(backend, pr_context, action_type)
        return result

    @staticmethod
    def _record_cost(backend: LLMBackend, pr_context: PRContext, action_type: str) -> None:
        record = getattr(backend, "record_cost", None)
        if not callable(record):
            return
        try:
            record(pr_context.project_id, pr_context.pr_id, action_type)
        except Exception:
            logger.debug("RLM: failed to record leaf cost.", exc_info=True)
