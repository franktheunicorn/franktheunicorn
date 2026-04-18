"""Discover available models from LLM provider APIs.

Used by the setup wizard to let users pick from models actually available
on their account / endpoint rather than guessing model names.

Each ``list_models_*`` function returns ``(models, status)`` where *status*
is ``"ok"`` when models were found, ``"empty"`` when the API responded
successfully but returned no models, or ``"error"`` on failure (missing
SDK, bad key, network error, etc.).
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

ListingStatus = Literal["ok", "empty", "error"]

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
) -> tuple[list[DiscoveredModel], ListingStatus]:
    """List models from the Anthropic API."""
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        return [], "error"
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
        return models, "ok" if models else "empty"
    except Exception:
        logger.debug("Failed to list Anthropic models", exc_info=True)
        return [], "error"


def list_models_openai(
    api_key_env: str = "OPENAI_API_KEY",
    base_url: str = "",
) -> tuple[list[DiscoveredModel], ListingStatus]:
    """List models from the OpenAI API (or any compatible endpoint)."""
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        return [], "error"
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
        return models, "ok" if models else "empty"
    except Exception:
        logger.debug("Failed to list OpenAI models", exc_info=True)
        return [], "error"


def list_models_gemini(
    api_key_env: str = "GOOGLE_API_KEY",
) -> tuple[list[DiscoveredModel], ListingStatus]:
    """List models from the Google Gemini API."""
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        return [], "error"
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
        return models, "ok" if models else "empty"
    except Exception:
        logger.debug("Failed to list Gemini models", exc_info=True)
        return [], "error"


def list_models_ollama(
    base_url: str = "",
) -> tuple[list[DiscoveredModel], ListingStatus]:
    """List locally available models from an Ollama server."""
    try:
        import ollama

        client = ollama.Client(host=base_url or None)
        response = client.list()
        models = []
        raw = getattr(response, "models", None)
        model_list = raw if raw is not None else response
        for m in model_list:
            model_id = getattr(m, "model", None) or getattr(m, "name", "") or ""
            if model_id:
                models.append(DiscoveredModel(model_id=model_id, display_name=model_id))
        models.sort(key=lambda m: m.model_id)
        return models, "ok" if models else "empty"
    except Exception:
        logger.debug("Failed to list Ollama models", exc_info=True)
        return [], "error"


def discover_models(
    provider: str,
    api_key_env: str = "",
    base_url: str = "",
) -> tuple[list[DiscoveredModel], ListingStatus]:
    """Discover models for a given provider.

    Parameters
    ----------
    provider:
        Provider name (``claude``, ``openai``, ``gemini``, ``ollama``).
    api_key_env:
        Override for the API key environment variable name.
    base_url:
        Base URL for OpenAI-compatible or Ollama endpoints.

    Returns ``(models, status)`` where *status* is ``"ok"``, ``"empty"``,
    or ``"error"``.
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
    return [], "error"


# Default base URLs used by each provider's SDK when none is specified.
_DEFAULT_BASE_URLS: dict[str, str] = {
    "claude": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com",
    "ollama": "http://localhost:11434",
}


def check_endpoint_reachability(url: str) -> str:
    """Check whether the hostname in *url* resolves via DNS.

    Returns an empty string if the host resolves, or a human-readable
    diagnostic message if it does not.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return f"Could not parse hostname from URL: {url}"

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return (
            f"Could not resolve hostname '{hostname}' \u2014 check your network connection or VPN"
        )
    except OSError as exc:
        return f"Network error resolving '{hostname}': {exc}"
    return ""


def discover_models_verbose(
    provider: str,
    api_key_env: str = "",
    base_url: str = "",
) -> tuple[list[DiscoveredModel], str]:
    """Like :func:`discover_models` but returns a diagnostic on failure.

    Returns ``(models, "")`` on success, or ``([], diagnostic)`` when model
    listing fails.  The diagnostic includes the URL that was attempted and
    a reachability check of the host.
    """
    models, status = discover_models(provider=provider, api_key_env=api_key_env, base_url=base_url)
    if models:
        return models, ""

    if status == "empty":
        return [], (
            "Listing models failed \u2014 the API returned no models.\n"
            "Please enter a model name manually."
        )

    # Build diagnostic for error cases.
    effective_url = base_url or _DEFAULT_BASE_URLS.get(provider, "")
    if not effective_url:
        return [], "Unknown provider and no base URL specified."

    parts: list[str] = []
    # Show the URL that was attempted.
    if provider == "ollama":
        models_path = "/api/tags"
    elif provider == "openai":
        # OpenAI-compatible clients use a base URL already rooted at .../v1
        # and request /models from there.
        models_path = "/models"
    else:
        models_path = "/v1/models"
    attempted = effective_url.rstrip("/") + models_path
    parts.append(f"Attempted: GET {attempted}")

    # Check reachability.
    reachability = check_endpoint_reachability(effective_url)
    if reachability:
        parts.append(reachability)
    else:
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        if not api_key and provider != "ollama":
            parts.append(
                f"Host is reachable but {api_key_env or 'API key'} is empty"
                " \u2014 check your API key"
            )
        else:
            parts.append(
                "Host is reachable but model listing failed"
                " \u2014 check your API key and endpoint path"
            )

    return [], "\n".join(parts)


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
