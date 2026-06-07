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

    def _leaf_name(self) -> str:
        return self._leaf_config.model or self._leaf_config.provider

    def _build_model_configs(self) -> dict[str, LLMBackendConfig]:
        """Map model-name → backend config for every model the RLM may call.

        In notebook mode the recursive driver can call *any* of these via
        ``llm(prompt, model=...)``. We gather all of the operator's configured
        backends plus the RLM's own leaf, skipping the ``rlm`` provider itself
        (an RLM can't be a leaf model — that would recurse forever).
        """
        configs: dict[str, LLMBackendConfig] = {}
        try:
            from franktheunicorn.config.loader import get_operator_config

            operator_config = get_operator_config()
            for backend in operator_config.llm_backends:
                if backend.provider == "rlm":
                    continue
                configs[backend.model or backend.provider] = backend
        except Exception:
            logger.debug("RLM: could not load operator config for model map.", exc_info=True)

        configs.setdefault(self._leaf_name(), self._leaf_config)
        return configs

    def _make_engine(self) -> RLMEngine:
        return RLMEngine(
            self._rlm_config,
            self._make_leaf,
            model_configs=self._build_model_configs(),
            default_model=self._leaf_name(),
        )

    def generate_review(self, diff: str, pr_context: PRContext) -> ReviewResult:
        return self._make_engine().review(diff, pr_context)

    def generate_findings(self, diff: str, pr_context: PRContext) -> list[ReviewFinding]:
        return self.generate_review(diff, pr_context).findings

    def complete(self, prompt: str, *, system: str = "") -> str:
        # An RLM is an orchestrator, not a raw-completion model; delegate to
        # its leaf so it still satisfies the LLMBackend protocol.
        return self._make_leaf().complete(prompt, system=system)
