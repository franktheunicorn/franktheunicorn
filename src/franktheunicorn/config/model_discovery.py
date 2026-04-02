"""Discover available models from LLM provider APIs.

Used by the setup wizard to let users pick from models actually available
on their account / endpoint rather than guessing model names.

Each ``list_models_*`` function returns a list of model ID strings or
an empty list on failure (missing SDK, bad key, network error, etc.).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveredModel:
    """A model available from a provider."""

    model_id: str
    """Full model identifier, e.g. ``claude-sonnet-4-20250514``."""

    display_name: str
    """Human-friendly name for the menu, e.g. ``claude-sonnet-4-20250514``."""


def list_models_anthropic(
    api_key_env: str = "ANTHROPIC_API_KEY",
) -> list[DiscoveredModel]:
    """List models from the Anthropic API."""
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        return []
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        page = client.models.list(limit=100)
        models = []
        for m in page.data:
            model_id = m.id
            display = getattr(m, "display_name", model_id) or model_id
            models.append(DiscoveredModel(model_id=model_id, display_name=display))
        models.sort(key=lambda m: m.model_id)
        return models
    except Exception:
        logger.debug("Failed to list Anthropic models", exc_info=True)
        return []


def list_models_openai(
    api_key_env: str = "OPENAI_API_KEY",
    base_url: str = "",
) -> list[DiscoveredModel]:
    """List models from the OpenAI API (or any compatible endpoint)."""
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        return []
    try:
        import openai

        kwargs: dict[str, object] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = openai.OpenAI(**kwargs)  # type: ignore[arg-type]
        response = client.models.list()
        models = []
        for m in response.data:
            models.append(DiscoveredModel(model_id=m.id, display_name=m.id))
        models.sort(key=lambda m: m.model_id)
        return models
    except Exception:
        logger.debug("Failed to list OpenAI models", exc_info=True)
        return []


def list_models_gemini(
    api_key_env: str = "GOOGLE_API_KEY",
) -> list[DiscoveredModel]:
    """List models from the Google Gemini API."""
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        return []
    try:
        from google import genai

        client = genai.Client(api_key=api_key)
        models = []
        for m in client.models.list():
            model_id = m.name or ""
            # Google model names are like "models/gemini-2.5-flash"; strip prefix.
            if model_id.startswith("models/"):
                model_id = model_id[len("models/") :]
            display = getattr(m, "display_name", model_id) or model_id
            models.append(DiscoveredModel(model_id=model_id, display_name=display))
        models.sort(key=lambda m: m.model_id)
        return models
    except Exception:
        logger.debug("Failed to list Gemini models", exc_info=True)
        return []


def list_models_ollama(
    base_url: str = "",
) -> list[DiscoveredModel]:
    """List locally available models from an Ollama server."""
    try:
        import ollama

        client = ollama.Client(host=base_url or None)
        response = client.list()
        models = []
        model_list = getattr(response, "models", None) or response
        for m in model_list:
            model_id = getattr(m, "model", None) or getattr(m, "name", "") or ""
            if model_id:
                models.append(DiscoveredModel(model_id=model_id, display_name=model_id))
        models.sort(key=lambda m: m.model_id)
        return models
    except Exception:
        logger.debug("Failed to list Ollama models", exc_info=True)
        return []


def discover_models(
    provider: str,
    api_key_env: str = "",
    base_url: str = "",
) -> list[DiscoveredModel]:
    """Discover models for a given provider.

    Parameters
    ----------
    provider:
        Provider name (``claude``, ``openai``, ``gemini``, ``ollama``).
    api_key_env:
        Override for the API key environment variable name.
    base_url:
        Base URL for OpenAI-compatible or Ollama endpoints.

    Returns an empty list if discovery fails or the provider is unknown.
    """
    if provider == "claude":
        return list_models_anthropic(api_key_env=api_key_env or "ANTHROPIC_API_KEY")
    if provider == "openai":
        return list_models_openai(
            api_key_env=api_key_env or "OPENAI_API_KEY",
            base_url=base_url,
        )
    if provider == "gemini":
        return list_models_gemini(api_key_env=api_key_env or "GOOGLE_API_KEY")
    if provider == "ollama":
        return list_models_ollama(base_url=base_url)
    return []


def format_model_menu(models: list[DiscoveredModel], max_display: int = 20) -> str:
    """Format a numbered menu of discovered models for interactive selection."""
    if not models:
        return ""
    lines = []
    shown = models[:max_display]
    for i, m in enumerate(shown, 1):
        if m.display_name != m.model_id:
            lines.append(f"    {i:3d}. {m.model_id}  ({m.display_name})")
        else:
            lines.append(f"    {i:3d}. {m.model_id}")
    if len(models) > max_display:
        lines.append(f"    ... and {len(models) - max_display} more")
    return "\n".join(lines)
