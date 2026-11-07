"""Difficulty-tiered model routing: which model handles which task.

Every LLM consumer names a *task* (``judge``, ``security_triage``,
``check:concurrency``...). A task has a default difficulty *tier*
(:data:`TASK_DEFAULT_TIERS`), the operator may re-point it
(``model_routing.tasks``), and a tier names a backend
(``model_routing.tiers``). A call site asks :func:`routed_backend` and gets
the tier's backend, or ``None`` when that tier is not configured — in which
case it does exactly what it did before routing existed. That is the whole
compatibility story: an operator.yaml with no ``model_routing`` block routes
nothing.

Fallback when the routed tier is missing: ``fast`` / ``reasoning`` / custom
tiers fall to ``standard``, and ``standard`` falls to the call site's own
default (``llm_backends[0]`` almost everywhere). So configuring only
``reasoning`` moves the hard tasks and leaves everything else alone.

A tier the worker's boot preflight disabled (:func:`mark_tier_disabled`) is
treated as unconfigured, so a dead reasoning model costs a fallback to the
review model rather than every triage call failing.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from franktheunicorn.config.models import (
    KNOWN_MODEL_TIERS,
    TIER_FAST,
    TIER_REASONING,
    TIER_STANDARD,
    ModelRoutingConfig,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig

logger = logging.getLogger(__name__)

#: The prefix that addresses one llm_check: ``check:concurrency``.
CHECK_TASK_PREFIX = "check:"
#: The task key that addresses every llm_check at once.
LLM_CHECK_TASK = "llm_check"

#: Built-in task -> tier. The rule of thumb: a call that answers yes/no or
#: rewrites text it was handed is ``fast``; a call that reads a diff and has to
#: find something is ``standard``; a call whose answer is a verdict somebody
#: acts on without re-deriving it (is this report real, should this dead PR be
#: revived) is ``reasoning``.
TASK_DEFAULT_TIERS: dict[str, str] = {
    # Screens and rewrites: small context, constrained output.
    "judge": TIER_FAST,
    "tone_guard": TIER_FAST,
    "worthiness_screen": TIER_FAST,
    "mergeability_triage": TIER_FAST,
    "check:pr-description": TIER_FAST,
    "check:malicious-prompt": TIER_FAST,
    # Diff reading.
    LLM_CHECK_TASK: TIER_STANDARD,
    "ci_analysis": TIER_STANDARD,
    "shepherding": TIER_STANDARD,
    # Verdicts.
    "security_triage": TIER_REASONING,
    "security_guidance": TIER_REASONING,
    "security_learning": TIER_REASONING,
    "zombie_assessment": TIER_REASONING,
}

_disabled_lock = threading.Lock()
_disabled_tiers: set[str] = set()


def mark_tier_disabled(tier: str) -> None:
    """Record that *tier*'s backend failed preflight; it routes as unconfigured."""
    with _disabled_lock:
        _disabled_tiers.add(tier)


def reset_disabled_tiers() -> None:
    """Clear preflight state (tests, and a worker restart)."""
    with _disabled_lock:
        _disabled_tiers.clear()


def is_tier_disabled(tier: str) -> bool:
    with _disabled_lock:
        return tier in _disabled_tiers


def tier_for(operator_config: OperatorConfig, task: str) -> str:
    """The tier *task* routes to: operator override, then built-in default.

    ``check:<name>`` consults its own key first, then ``llm_check``, so
    ``llm_check: reasoning`` moves every check and ``check:pr-description:
    fast`` still pins the one that does not need it.
    """
    overrides = operator_config.model_routing.tasks
    if task in overrides:
        return overrides[task]
    is_check = task.startswith(CHECK_TASK_PREFIX)
    if is_check and task not in TASK_DEFAULT_TIERS and LLM_CHECK_TASK in overrides:
        return overrides[LLM_CHECK_TASK]
    if task in TASK_DEFAULT_TIERS:
        return TASK_DEFAULT_TIERS[task]
    if is_check:
        return TASK_DEFAULT_TIERS[LLM_CHECK_TASK]
    return TIER_STANDARD


def _tier_backend(operator_config: OperatorConfig, tier: str) -> LLMBackendConfig | None:
    if is_tier_disabled(tier):
        return None
    return operator_config.model_routing.tiers.get(tier)


def routed_backend(operator_config: OperatorConfig | None, task: str) -> LLMBackendConfig | None:
    """The configured backend for *task*'s tier, or ``None`` to use the default.

    ``None`` means "routing has no opinion" — the caller then uses whatever it
    used before (``llm_backends[0]``, its own fallthrough list...). A missing
    non-standard tier tries ``standard`` first, so a configured standard tier
    is honoured by tasks whose own tier is unset.
    """
    # isinstance, not truthiness: callers pass test doubles and partially-built
    # configs, and a Mock's ``.tiers.get`` would hand back a Mock "backend".
    routing = getattr(operator_config, "model_routing", None)
    if operator_config is None or not isinstance(routing, ModelRoutingConfig):
        return None
    if not routing.tiers:
        return None
    tier = tier_for(operator_config, task)
    cfg = _tier_backend(operator_config, tier)
    if cfg is None and tier != TIER_STANDARD:
        cfg = _tier_backend(operator_config, TIER_STANDARD)
    if cfg is not None:
        logger.debug("Model routing: %s -> %s (%s/%s)", task, tier, cfg.provider, cfg.model)
    return cfg


def with_routed_first(
    operator_config: OperatorConfig | None,
    task: str,
    fallbacks: list[LLMBackendConfig],
) -> list[LLMBackendConfig]:
    """``[routed backend, *fallbacks]`` for call sites that try backends in order.

    The routed tier is tried first and the old list stays behind it, so a
    reasoning model that is rate-limited mid-run costs a fallthrough to the
    review model, not a failed verdict. No dedup here: the fallthrough callers
    already dedup on connection identity, and the ones that don't stop at the
    first answer anyway.
    """
    routed = routed_backend(operator_config, task)
    return [routed, *fallbacks] if routed is not None else list(fallbacks)


def describe_routing(operator_config: OperatorConfig) -> list[str]:
    """One line per task saying where it goes — the startup routing table.

    Unconfigured routing is said once rather than not at all: "everything is on
    llm_backends[0]" is the answer to "why is triage on the small model".
    """
    routing = operator_config.model_routing
    if not routing.tiers:
        return [
            "Model routing: no model_routing.tiers configured — every task uses its "
            "default backend (llm_backends[0] unless the feature sets its own)."
        ]
    default = operator_config.llm_backends[0] if operator_config.llm_backends else None
    default_label = f"{default.provider}/{default.model or 'default'}" if default else "(none)"
    lines = []
    for name, tier_cfg in sorted(routing.tiers.items()):
        state = " [DISABLED by preflight]" if is_tier_disabled(name) else ""
        lines.append(f"Model tier {name}: {tier_cfg.provider}/{tier_cfg.model or 'default'}{state}")
    tasks = sorted({*TASK_DEFAULT_TIERS, *routing.tasks})
    for task in tasks:
        tier = tier_for(operator_config, task)
        cfg = routed_backend(operator_config, task)
        target = f"{cfg.provider}/{cfg.model or 'default'}" if cfg else f"default {default_label}"
        lines.append(f"  {task} -> {tier}: {target}")
    return lines


def routing_warnings(operator_config: OperatorConfig) -> list[str]:
    """Config mistakes that would otherwise route silently to the default."""
    routing = operator_config.model_routing
    warnings: list[str] = []
    known_tiers = {*KNOWN_MODEL_TIERS, *routing.tiers}
    for task, tier in sorted(routing.tasks.items()):
        if task not in TASK_DEFAULT_TIERS and not task.startswith(CHECK_TASK_PREFIX):
            warnings.append(
                f"model_routing.tasks names unknown task {task!r}; known tasks: "
                f"{', '.join(sorted(TASK_DEFAULT_TIERS))}, or check:<name>."
            )
        if tier not in known_tiers:
            warnings.append(
                f"model_routing.tasks routes {task!r} to tier {tier!r}, which is not "
                f"defined under model_routing.tiers; it falls back to standard."
            )
    if routing.tiers and not operator_config.llm_backends:
        for name, cfg in routing.tiers.items():
            if cfg.provider == "stub":
                warnings.append(
                    f"model_routing.tiers.{name} has no provider and there is no "
                    "llm_backends[0] to inherit one from, so it is the stub backend."
                )
    return warnings
