"""LLM backend registry — maps provider names to backend instances."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import LLMBackend

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig

logger = logging.getLogger(__name__)


def get_backend(config: LLMBackendConfig) -> LLMBackend:
    """Return the appropriate LLM backend for the given config.

    Falls back to the stub backend if the provider is unknown or its
    SDK is not installed.
    """
    provider = config.provider.lower()

    if provider == "claude":
        from franktheunicorn.review.backends.claude_backend import ClaudeBackend

        return ClaudeBackend(config)

    if provider == "openai":
        from franktheunicorn.review.backends.openai_backend import OpenAIBackend

        return OpenAIBackend(config)

    if provider == "gemini":
        from franktheunicorn.review.backends.gemini_backend import GeminiBackend

        return GeminiBackend(config)

    if provider == "ollama":
        from franktheunicorn.review.backends.ollama_backend import OllamaBackend

        return OllamaBackend(config)

    from franktheunicorn.review.backends.stub_backend import StubBackend

    if provider != "stub":
        logger.warning("Unknown LLM provider '%s'; falling back to stub.", provider)
    return StubBackend(config)


__all__ = ["LLMBackend", "get_backend"]
