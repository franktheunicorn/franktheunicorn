"""Per-backend smoke test: one cheap live probe that 86s a dead backend at boot.

Used by the worker's startup preflight and by triage's backend-health seed, so a
backend that 403s or whose Ollama isn't running is disabled once — at boot —
rather than 86 times across a 234-report backlog. The probe is one cheap call per
backend (``models.list()`` for the cloud providers, ``list()`` for Ollama); a
backend that passes here and goes down mid-run is caught by the runtime circuit
breaker in ``security.triage``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from franktheunicorn.config.models import LLMBackendConfig

logger = logging.getLogger(__name__)

#: Providers that authenticate through an env var the preflight reads. A
#: provider not listed here is treated as keyless (Ollama, stub, llama.cpp, vllm,
#: unknown) and skips the missing-key check — guessing "it needs one" disables a
#: working keyless backend.
_KEYED_PROVIDERS: frozenset[str] = frozenset({"claude", "openai", "gemini"})

#: Providers that need no API key but may still need a live server (Ollama is
#: probed separately; stub/llama-cpp/vllm have no cheap probe and stay unchecked).
_KEYLESS_PROVIDERS: frozenset[str] = frozenset({"stub", "ollama", "llama-cpp", "vllm"})

#: Default env var per keyed provider, for backends that don't set api_key_env.
_DEFAULT_KEY_ENV: dict[str, str] = {
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
}


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of one backend probe."""

    ok: bool
    #: Why it failed, for the log. Empty when ok.
    reason: str = ""
    #: For openai-compatible endpoints without ``/models``: the token param the
    #: chat probe succeeded with, so the caller can seed ``LLMBackendFallback``.
    token_param: str | None = None
    #: Whether a live call was actually made. False for providers with no probe
    #: (stub, llama-cpp, vllm, unknown) — "unchecked", not "OK".
    probed: bool = True


def needs_api_key(provider: str) -> bool:
    """Whether this provider authenticates through an env var this preflight reads.

    Asks the backend class, falling back to the static list for providers whose
    modules are optional installs. A provider whose backend declares no
    ``_default_key_env`` and takes no ``api_key_env`` is not something a
    missing-key check can rule on, and guessing "it needs one" disables a
    working backend.
    """
    if provider in _KEYLESS_PROVIDERS:
        return False
    try:
        from franktheunicorn.review.backends import _BACKENDS

        entry = _BACKENDS.get(provider)
        if entry is None:
            return False
        import importlib

        backend_cls = getattr(importlib.import_module(entry[0]), entry[1])
        return bool(getattr(backend_cls, "_default_key_env", ""))
    except Exception:
        logger.debug("Could not introspect backend for provider %r", provider, exc_info=True)
        return True


def default_key_env(provider: str) -> str:
    return _DEFAULT_KEY_ENV.get(provider, "")


def probe_llm_backend(cfg: LLMBackendConfig) -> ProbeResult:
    """One cheap live call against *cfg*'s provider.

    Returns ``ProbeResult(ok=True)`` for providers with no probe (stub, llama-cpp,
    vllm, unknown): nothing was proven, so a bad key only surfaces at the first
    real call — but Ollama (the common keyless case) gets a real probe now
    instead of being silently "unchecked" and failing on every report.
    """
    import os

    provider = cfg.provider.lower()
    key_env = cfg.api_key_env or default_key_env(provider)
    api_key = os.environ.get(key_env, "") if key_env else ""

    if provider == "ollama":
        return _probe_ollama(cfg)
    if not needs_api_key(provider):
        # stub / llama-cpp / vllm / unknown: no probe, assume OK.
        return ProbeResult(ok=True, probed=False)

    if not api_key:
        return ProbeResult(ok=False, reason=f"no API key in env var {key_env or '(unset)'!r}")

    if provider == "claude":
        return _probe_claude(api_key)
    if provider == "openai":
        return _probe_openai(cfg, api_key)
    if provider == "gemini":
        return _probe_gemini(api_key)
    return ProbeResult(ok=True)


def _probe_claude(api_key: str) -> ProbeResult:
    import anthropic

    try:
        anthropic.Anthropic(api_key=api_key).models.list()
    except Exception as exc:
        return ProbeResult(ok=False, reason=f"{type(exc).__name__}: {exc}")
    return ProbeResult(ok=True)


def _probe_openai(cfg: LLMBackendConfig, api_key: str) -> ProbeResult:
    import openai

    kwargs: dict[str, str] = {"api_key": api_key}
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    client = openai.OpenAI(**kwargs)  # type: ignore[arg-type]
    model = cfg.model or "gpt-4o"
    try:
        client.models.list()
    except openai.NotFoundError:
        # OpenAI-compatible endpoints that don't implement /models (e.g. Cortex):
        # fall back to a minimal chat call. max_tokens first; if the server rejects
        # it with a deprecation error, retry with max_completion_tokens.
        return _openai_chat_probe(client, model)
    except Exception as exc:
        return ProbeResult(ok=False, reason=f"{type(exc).__name__}: {exc}")
    return ProbeResult(ok=True)


def _openai_chat_probe(client: Any, model: str) -> ProbeResult:
    import openai

    token_param = "max_tokens"
    for attempt in range(2):
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                **{token_param: 1},
            )
        except openai.BadRequestError as exc:
            msg = str(exc).lower()
            if attempt == 0 and ("max_tokens" in msg or "max_completion_tokens" in msg):
                token_param = "max_completion_tokens"
                continue
            return ProbeResult(ok=False, reason=f"BadRequestError: {exc}")
        except Exception as exc:
            return ProbeResult(ok=False, reason=f"{type(exc).__name__}: {exc}")
        else:
            # Only surface the token param when the retry changed it from the
            # default — seeding "max_tokens" would write a fallback row at every
            # boot for an endpoint that already accepts the default, which the
            # old preflight deliberately avoided.
            return ProbeResult(
                ok=True, token_param=token_param if token_param != "max_tokens" else None
            )
    # Unreachable: every loop iteration returns. Kept so mypy sees a complete
    # return path on this function (the for-loop's exits are not provably total).
    return ProbeResult(ok=False, reason="openai chat probe did not return")


def _probe_gemini(api_key: str) -> ProbeResult:
    from google import genai

    try:
        genai.Client(api_key=api_key).models.list()
    except Exception as exc:
        return ProbeResult(ok=False, reason=f"{type(exc).__name__}: {exc}")
    return ProbeResult(ok=True)


def _probe_ollama(cfg: LLMBackendConfig) -> ProbeResult:
    """Ollama used to be "unchecked" — no probe — so a dead Ollama passed boot and
    failed on every report. ``client.list()`` is the cheap reachability probe: it
    lists local models without loading one, so a server that's up answers in
    milliseconds and one that's down raises ConnectionError here, not 86 times
    across the backlog."""
    import ollama

    try:
        ollama.Client(host=cfg.base_url or None).list()
    except Exception as exc:
        return ProbeResult(ok=False, reason=f"{type(exc).__name__}: {exc}")
    return ProbeResult(ok=True)
