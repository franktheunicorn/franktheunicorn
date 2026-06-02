"""The ``rlm`` LLM backend — a Recursive Language Model over an existing backend.

``RLMBackend`` satisfies the :class:`LLMBackend` protocol like any other
backend, so the drafter dispatches to it through the normal registry with no
special-casing. It owns no SDK of its own: every leaf review is delegated to
an ordinary backend resolved from ``config.rlm.leaf``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from franktheunicorn.review.rlm.engine import RLMEngine

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.review.backends.base import (
        LLMBackend,
        PRContext,
        ReviewFinding,
        ReviewResult,
    )

logger = logging.getLogger(__name__)


class RLMBackend:
    """Recursive review backend (provider ``rlm``)."""

    def __init__(self, config: LLMBackendConfig) -> None:
        from franktheunicorn.config.models import LLMBackendConfig as _LLMBackendConfig
        from franktheunicorn.config.models import RLMConfig

        self._config = config
        self._rlm_config = config.rlm or RLMConfig()

        leaf = self._rlm_config.leaf
        # Guard against an rlm-leaf configured as rlm (infinite recursion).
        if leaf.provider == "rlm":
            logger.warning("RLM leaf provider cannot be 'rlm'; falling back to stub leaf.")
            leaf = _LLMBackendConfig(provider="stub")
        self._leaf_config = leaf

    def _make_leaf(self) -> LLMBackend:
        from franktheunicorn.review.backends import get_backend

        return get_backend(self._leaf_config)

    def generate_review(self, diff: str, pr_context: PRContext) -> ReviewResult:
        engine = RLMEngine(self._rlm_config, self._make_leaf)
        return engine.review(diff, pr_context)

    def generate_findings(self, diff: str, pr_context: PRContext) -> list[ReviewFinding]:
        return self.generate_review(diff, pr_context).findings
