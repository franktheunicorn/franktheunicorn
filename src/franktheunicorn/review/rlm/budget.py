"""Recursion budget for the RLM engine.

The budget is the sole guard against runaway cost: it caps the number of
leaf LLM calls and the total estimated tokens spent on a single PR. Once
either ceiling is reached the engine stops dispatching new leaves and
returns whatever it has gathered so far.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (``len // 4``), matching ``ContextConfig``."""
    return len(text) // 4


@dataclass
class RLMBudget:
    """Mutable, thread-safe-enough budget tracker for one RLM run.

    The engine fans leaves out with a thread pool, but ``charge`` is only
    called from the worker threads to *reserve* a call before dispatch; the
    small race at the boundary can let one extra call through, which is
    harmless given the caps are advisory cost guards, not hard limits.
    """

    max_sub_calls: int
    total_token_budget: int
    sub_calls_used: int = 0
    tokens_used: int = 0
    exhausted_logged: bool = False

    def can_afford(self, est_tokens: int) -> bool:
        """Return True if another leaf call of ``est_tokens`` fits the budget."""
        if self.sub_calls_used >= self.max_sub_calls:
            self._warn_once("max_sub_calls")
            return False
        if self.tokens_used + est_tokens > self.total_token_budget:
            self._warn_once("total_token_budget")
            return False
        return True

    def charge(self, est_tokens: int) -> None:
        """Record one leaf call against the budget."""
        self.sub_calls_used += 1
        self.tokens_used += est_tokens

    def _warn_once(self, which: str) -> None:
        if not self.exhausted_logged:
            logger.warning(
                "RLM budget exhausted (%s): %d/%d sub-calls, ~%d/%d tokens — "
                "returning partial findings.",
                which,
                self.sub_calls_used,
                self.max_sub_calls,
                self.tokens_used,
                self.total_token_budget,
            )
            self.exhausted_logged = True
