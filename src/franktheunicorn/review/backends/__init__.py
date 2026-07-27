"""LLM backend registry — maps provider names to backend instances."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import LLMBackend

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig

logger = logging.getLogger(__name__)

# Lazy-import map: provider name → (module_path, class_name)
_BACKENDS: dict[str, tuple[str, str]] = {
    "claude": ("franktheunicorn.review.backends.claude_backend", "ClaudeBackend"),
    "openai": ("franktheunicorn.review.backends.openai_backend", "OpenAIBackend"),
    "gemini": ("franktheunicorn.review.backends.gemini_backend", "GeminiBackend"),
    "ollama": ("franktheunicorn.review.backends.ollama_backend", "OllamaBackend"),
    "stub": ("franktheunicorn.review.backends.stub_backend", "StubBackend"),
    "rlm": ("franktheunicorn.review.rlm.backend", "RLMBackend"),
}


def get_backend(config: LLMBackendConfig) -> LLMBackend:
    """Return the appropriate LLM backend for the given config.

    Falls back to the stub backend if the provider is unknown.
    """
    import importlib

    provider = config.provider.lower()
    entry = _BACKENDS.get(provider)
    if entry is None:
        logger.warning("Unknown LLM provider '%s'; falling back to stub.", provider)
        entry = _BACKENDS["stub"]

    module_path, class_name = entry
    module = importlib.import_module(module_path)
    backend_cls = getattr(module, class_name)
    return backend_cls(config)  # type: ignore[no-any-return]


__all__ = ["LLMBackend", "get_backend"]
