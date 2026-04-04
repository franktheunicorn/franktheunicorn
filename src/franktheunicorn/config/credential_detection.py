"""Detect LLM API credentials and endpoints from environment variables.

Scans ``os.environ`` across three confidence tiers and returns structured
results that the setup wizard can present to the user.

This module is intentionally pure — it takes an environ dict parameter
(defaulting to ``os.environ``) so it can be tested without side effects.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectedCredential:
    """A single credential or endpoint found in the environment."""

    env_var: str
    """Environment variable name, e.g. ``ANTHROPIC_API_KEY``."""

    value_preview: str
    """Masked preview of the value, e.g. ``sk-ant-****``."""

    provider: str
    """Known provider name (``claude``, ``openai``, …) or ``""`` for unknown."""

    confidence: str
    """Detection confidence: ``high``, ``medium``, or ``low``."""

    credential_type: str
    """One of ``api_key``, ``endpoint``, or ``token``."""

    paired_with: str
    """Env var name of a paired credential/endpoint, or ``""``."""


@dataclass(frozen=True)
class DynamicMenuEntry:
    """A detected credential promoted to a selectable menu entry in the setup wizard."""

    key: str
    """Menu key, e.g. ``"8"``."""

    label: str
    """Display label, e.g. ``"groq"`` or ``"cortex"``."""

    api_key_env: str
    """Environment variable name for the API key or token."""

    base_url_env: str
    """Environment variable name for the endpoint URL, or ``""``."""

    provider_hint: str
    """Original provider field from the detection, or ``""``."""


# ---------------------------------------------------------------------------
# Tier 1: exact match → native providers
# ---------------------------------------------------------------------------

# Maps env var name → (provider, credential_type)
TIER1_MAP: dict[str, tuple[str, str]] = {
    "ANTHROPIC_API_KEY": ("claude", "api_key"),
    "OPENAI_API_KEY": ("openai", "api_key"),
    "GOOGLE_API_KEY": ("gemini", "api_key"),
}

# GitHub tokens — detected in the bash script flow for FRANK_GITHUB_TOKEN.
GITHUB_TOKEN_VARS: dict[str, tuple[str, str]] = {
    "GITHUB_TOKEN": ("github", "token"),
    "GH_TOKEN": ("github", "token"),
}

# ---------------------------------------------------------------------------
# Tier 2: known third-party LLM providers
# ---------------------------------------------------------------------------

TIER2_MAP: dict[str, tuple[str, str]] = {
    "MISTRAL_API_KEY": ("mistral", "api_key"),
    "DEEPSEEK_API_KEY": ("deepseek", "api_key"),
    "GROQ_API_KEY": ("groq", "api_key"),
    "TOGETHER_API_KEY": ("together", "api_key"),
    "TOGETHER_AI_API_KEY": ("together", "api_key"),
    "FIREWORKS_API_KEY": ("fireworks", "api_key"),
    "REPLICATE_API_TOKEN": ("replicate", "token"),
    "COHERE_API_KEY": ("cohere", "api_key"),
    "AI21_API_KEY": ("ai21", "api_key"),
    "AZURE_OPENAI_API_KEY": ("azure-openai", "api_key"),
    "AZURE_OPENAI_ENDPOINT": ("azure-openai", "endpoint"),
    "HF_TOKEN": ("huggingface", "token"),
    "HUGGING_FACE_HUB_TOKEN": ("huggingface", "token"),
}

# ---------------------------------------------------------------------------
# Tier 3: fuzzy heuristic patterns
# ---------------------------------------------------------------------------

ENDPOINT_NAME_SUFFIXES: tuple[str, ...] = (
    "_BASE_URL",
    "_URL",
    "_ENDPOINT",
    "_HOST",
    "_BASE",
)

CREDENTIAL_NAME_SUFFIXES: tuple[str, ...] = (
    "_API_KEY",
    "_API_TOKEN",
    "_KEY",
    "_TOKEN",
    "_PAT",
    "_SECRET",
    "_AUTH",
)

# Values that look like OpenAI-compatible endpoints.
_ENDPOINT_VALUE_RE = re.compile(
    r"/api/.*(/v1|/chat/completions)"
    r"|/v1(/|$)"
    r"|/chat/completions",
)

# Common API key value prefixes.
_KEY_VALUE_PREFIXES: tuple[str, ...] = (
    "sk-",
    "key-",
    "pk-",
    "rk-",
    "whsec_",
    "gsk_",
    "xai-",
    "pplx-",
)

# Env vars to exclude from Tier 3 (not LLM-related).
_TIER3_EXCLUDE_PREFIXES: tuple[str, ...] = (
    "SSH_",
    "GPG_",
    "DBUS_",
    "XDG_",
    "DOCKER_",
    "npm_",
    "CARGO_",
    "RUST",
    "NODE_",
    "JAVA_",
    "PYENV_",
    "VIRTUAL_ENV",
    "CONDA_",
)


# ---------------------------------------------------------------------------
# Provider → menu key mapping (matches setup_llm._PROVIDERS)
# ---------------------------------------------------------------------------

_PROVIDER_TO_MENU_KEY: dict[str, str] = {
    "claude": "1",
    "openai": "2",
    "gemini": "3",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def mask_value(value: str) -> str:
    """Return a masked preview of *value* safe for display.

    Shows the first 6 characters followed by ``****``.  Values with
    4 or fewer characters are fully masked.
    """
    if len(value) <= 4:
        return "****"
    return value[:6] + "****"


def detect_llm_credentials(
    environ: dict[str, str] | None = None,
    *,
    include_github: bool = False,
) -> list[DetectedCredential]:
    """Scan environment variables for LLM API credentials and endpoints.

    Parameters
    ----------
    environ:
        Mapping to scan.  Defaults to ``os.environ``.
    include_github:
        If ``True``, also detect ``GITHUB_TOKEN`` / ``GH_TOKEN``.

    Returns a list sorted by confidence (high → low), then alphabetically.
    """
    env = environ if environ is not None else dict(os.environ)
    seen: set[str] = set()
    results: list[DetectedCredential] = []

    # --- Tier 1: exact known providers ---
    for var, (provider, ctype) in TIER1_MAP.items():
        val = env.get(var, "")
        if val:
            results.append(
                DetectedCredential(
                    env_var=var,
                    value_preview=mask_value(val),
                    provider=provider,
                    confidence="high",
                    credential_type=ctype,
                    paired_with="",
                )
            )
            seen.add(var)

    if include_github:
        for var, (provider, ctype) in GITHUB_TOKEN_VARS.items():
            if var in seen:
                continue
            val = env.get(var, "")
            if val:
                results.append(
                    DetectedCredential(
                        env_var=var,
                        value_preview=mask_value(val),
                        provider=provider,
                        confidence="high",
                        credential_type=ctype,
                        paired_with="",
                    )
                )
                seen.add(var)

    # --- Tier 2: known third-party ---
    for var, (provider, ctype) in TIER2_MAP.items():
        if var in seen:
            continue
        val = env.get(var, "")
        if val:
            results.append(
                DetectedCredential(
                    env_var=var,
                    value_preview=mask_value(val),
                    provider=provider,
                    confidence="medium",
                    credential_type=ctype,
                    paired_with="",
                )
            )
            seen.add(var)

    # --- Tier 3: fuzzy heuristic ---
    # Two-pass approach: first detect endpoints (and pair with credentials),
    # then detect standalone credentials.  This ensures that a credential
    # paired with an endpoint isn't consumed as "standalone" first due to
    # alphabetical ordering.

    # Pass 1: endpoints
    for var, val in sorted(env.items()):
        if var in seen or not val:
            continue
        if any(var.startswith(prefix) for prefix in _TIER3_EXCLUDE_PREFIXES):
            continue

        if _is_endpoint_name(var) and _ENDPOINT_VALUE_RE.search(val):
            paired = _find_paired_credential(var, env, seen)
            results.append(
                DetectedCredential(
                    env_var=var,
                    value_preview=mask_value(val),
                    provider="",
                    confidence="low",
                    credential_type="endpoint",
                    paired_with=paired,
                )
            )
            seen.add(var)
            if paired:
                paired_val = env.get(paired, "")
                results.append(
                    DetectedCredential(
                        env_var=paired,
                        value_preview=mask_value(paired_val) if paired_val else "****",
                        provider="",
                        confidence="low",
                        credential_type="api_key",
                        paired_with=var,
                    )
                )
                seen.add(paired)

    # Pass 2: standalone credentials with key-looking values
    for var, val in sorted(env.items()):
        if var in seen or not val:
            continue
        if any(var.startswith(prefix) for prefix in _TIER3_EXCLUDE_PREFIXES):
            continue

        if _is_credential_name(var) and _has_key_prefix(val):
            results.append(
                DetectedCredential(
                    env_var=var,
                    value_preview=mask_value(val),
                    provider="",
                    confidence="low",
                    credential_type="api_key",
                    paired_with="",
                )
            )
            seen.add(var)

    # Sort: high first, then medium, then low; alphabetical within tier.
    order = {"high": 0, "medium": 1, "low": 2}
    results.sort(key=lambda d: (order.get(d.confidence, 9), d.env_var))
    return results


def suggest_provider_choices(detections: list[DetectedCredential]) -> str:
    """Return a comma-separated string of menu keys for detected providers.

    Only considers high-confidence detections that map to a native provider.
    Returns ``"5"`` (skip) if nothing matches.
    """
    keys: list[str] = []
    for d in detections:
        if d.confidence == "high" and d.provider in _PROVIDER_TO_MENU_KEY:
            key = _PROVIDER_TO_MENU_KEY[d.provider]
            if key not in keys:
                keys.append(key)
    return ",".join(keys) if keys else "7"


def format_detections(detections: list[DetectedCredential]) -> str:
    """Format detected credentials for display in the setup wizard."""
    if not detections:
        return ""

    lines: list[str] = []
    lines.append("Detected LLM credentials in your environment:\n")

    by_confidence: dict[str, list[DetectedCredential]] = {
        "high": [],
        "medium": [],
        "low": [],
    }
    for d in detections:
        by_confidence[d.confidence].append(d)

    if by_confidence["high"]:
        lines.append("  Found:")
        for d in by_confidence["high"]:
            label = f" ({d.provider})" if d.provider else ""
            lines.append(f"    {d.env_var} = {d.value_preview}{label}")

    if by_confidence["medium"]:
        lines.append("  Also found (OpenAI-compatible backend possible):")
        for d in by_confidence["medium"]:
            label = f" ({d.provider})" if d.provider else ""
            lines.append(f"    {d.env_var} = {d.value_preview}{label}")

    if by_confidence["low"]:
        lines.append("  Possible matches:")
        for d in by_confidence["low"]:
            extra = ""
            if d.paired_with:
                extra = f" (paired with {d.paired_with})"
            lines.append(f"    {d.env_var} = {d.value_preview}{extra}")

    lines.append("")
    return "\n".join(lines)


def get_openai_compatible_detections(
    detections: list[DetectedCredential],
) -> list[DetectedCredential]:
    """Return tier 2/3 detections that could be OpenAI-compatible backends."""
    native_providers = {"claude", "openai", "gemini", "github"}
    return [
        d
        for d in detections
        if d.confidence in ("medium", "low")
        and d.credential_type in ("api_key", "token", "endpoint")
        and d.provider not in native_providers
    ]


def derive_detection_label(detection: DetectedCredential) -> str:
    """Derive a human-readable label from a detected credential.

    Uses the provider name when available (Tier 2), otherwise strips the
    endpoint/credential suffix from the env var name and normalises it.

    Examples::

        GROQ_API_KEY          → "groq"
        CORTEX_URL    → "cortex"
        MY_LLM_BASE_URL       → "my-llm"
        CUSTOM_AI_API_KEY     → "custom-ai"
    """
    if detection.provider:
        return detection.provider

    name = detection.env_var
    # Strip suffixes, longest first to avoid partial matches
    # (e.g. _BASE_URL before _URL).
    all_suffixes = sorted(
        ENDPOINT_NAME_SUFFIXES + CREDENTIAL_NAME_SUFFIXES,
        key=len,
        reverse=True,
    )
    upper = name.upper()
    for suffix in all_suffixes:
        if upper.endswith(suffix):
            name = name[: -len(suffix)]
            break

    return name.lower().replace("_", "-").strip("-") or detection.env_var.lower()


def build_dynamic_menu_entries(
    detections: list[DetectedCredential],
    start_key: int = 8,
) -> list[DynamicMenuEntry]:
    """Build numbered menu entries for Tier 2/3 detected credentials.

    Groups paired endpoint+credential detections into a single entry and
    deduplicates by label so each provider appears at most once.

    Parameters
    ----------
    detections:
        Full detection list (all tiers).  Tier 1 is filtered out.
    start_key:
        First numeric menu key to assign (default ``8``).

    Returns a list of :class:`DynamicMenuEntry` with sequential keys.
    """
    compat = get_openai_compatible_detections(detections)
    if not compat:
        return []

    # Index by env_var for quick paired lookups.
    by_var: dict[str, DetectedCredential] = {d.env_var: d for d in compat}

    # Pre-group Tier 2 detections by provider so that e.g. AZURE_OPENAI_API_KEY
    # and AZURE_OPENAI_ENDPOINT (which lack paired_with) are merged.
    provider_endpoints: dict[str, str] = {}
    provider_keys: dict[str, str] = {}
    for d in compat:
        if d.provider and d.confidence == "medium":
            if d.credential_type == "endpoint":
                provider_endpoints.setdefault(d.provider, d.env_var)
            else:
                provider_keys.setdefault(d.provider, d.env_var)

    seen_labels: set[str] = set()
    seen_vars: set[str] = set()
    entries: list[DynamicMenuEntry] = []
    key = start_key

    for d in compat:
        if d.env_var in seen_vars:
            continue

        api_key_env = ""
        base_url_env = ""

        if d.credential_type == "endpoint":
            base_url_env = d.env_var
            if d.paired_with and d.paired_with in by_var:
                api_key_env = d.paired_with
                seen_vars.add(d.paired_with)
            elif d.provider and d.provider in provider_keys:
                # Tier 2 same-provider pairing (e.g. Azure OpenAI).
                api_key_env = provider_keys[d.provider]
                seen_vars.add(api_key_env)
        else:
            # api_key or token
            api_key_env = d.env_var
            if d.paired_with and d.paired_with in by_var:
                paired = by_var[d.paired_with]
                if paired.credential_type == "endpoint":
                    base_url_env = d.paired_with
                    seen_vars.add(d.paired_with)
            elif d.provider and d.provider in provider_endpoints:
                # Tier 2 same-provider pairing.
                base_url_env = provider_endpoints[d.provider]
                seen_vars.add(base_url_env)

        seen_vars.add(d.env_var)
        label = derive_detection_label(d)

        if label in seen_labels:
            continue
        seen_labels.add(label)

        entries.append(
            DynamicMenuEntry(
                key=str(key),
                label=label,
                api_key_env=api_key_env,
                base_url_env=base_url_env,
                provider_hint=d.provider,
            )
        )
        key += 1

    return entries


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_endpoint_name(name: str) -> bool:
    upper = name.upper()
    return any(upper.endswith(suffix) for suffix in ENDPOINT_NAME_SUFFIXES)


def _is_credential_name(name: str) -> bool:
    upper = name.upper()
    return "_API_KEY" in upper or "_API_TOKEN" in upper


def _has_key_prefix(value: str) -> bool:
    return any(value.startswith(prefix) for prefix in _KEY_VALUE_PREFIXES)


def _find_paired_credential(
    endpoint_var: str,
    env: dict[str, str],
    already_seen: set[str],
) -> str:
    """Given an endpoint env var name, find a plausible credential sibling."""
    # Strip the endpoint suffix to get the prefix.
    prefix = endpoint_var
    for suffix in ENDPOINT_NAME_SUFFIXES:
        if prefix.upper().endswith(suffix):
            prefix = prefix[: -len(suffix)]
            break

    if not prefix:
        return ""

    for cred_suffix in CREDENTIAL_NAME_SUFFIXES:
        candidate = prefix + cred_suffix
        if candidate not in already_seen and env.get(candidate, ""):
            return candidate
        # Also try the original casing patterns.
        candidate_upper = prefix.upper() + cred_suffix
        if (
            candidate_upper != candidate
            and candidate_upper not in already_seen
            and env.get(candidate_upper, "")
        ):
            return candidate_upper

    return ""
