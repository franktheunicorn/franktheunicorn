"""Extensible LLM sub-check system.

Each check asks an LLM to evaluate a specific aspect of a PR (coverage,
docs, API compat, etc.) and returns ReviewFindings that flow through
the standard anti-pattern gating and draft pipeline.

Adding a new check:
  1. Create ``review/checks/my_check.py`` with a class extending ``BaseCheck``
  2. Add the class to ``REGISTRY`` below
  3. Users enable it via ``llm_checks: [my_check]`` in their project YAML
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import PRContext, ReviewFinding, parse_llm_response
from franktheunicorn.review.drafter import build_pr_context, create_drafts_from_findings

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import PullRequest, ReviewDraft

logger = logging.getLogger(__name__)


class BaseCheck(ABC):
    """Base class for all LLM sub-checks.

    Subclasses must set ``name`` and implement ``build_prompt``.
    ``parse_response`` defaults to the standard ReviewFinding JSON parser.
    """

    name: str = ""

    @abstractmethod
    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        """Return (system_prompt, user_message) for this check."""

    def parse_response(self, raw_text: str) -> list[ReviewFinding]:
        """Parse LLM output into findings. Override for custom formats."""
        return parse_llm_response(raw_text)


def _get_registry() -> dict[str, type[BaseCheck]]:
    """Lazy registry to avoid circular imports at module level."""
    from franktheunicorn.review.checks.coverage import CoverageCheck
    from franktheunicorn.review.checks.issue_link import IssueLinkCheck
    from franktheunicorn.review.checks.malicious_prompt import MaliciousPromptCheck
    from franktheunicorn.review.checks.security import SecurityCheck
    from franktheunicorn.review.checks.security_context import SecurityContextCheck

    return {
        "coverage": CoverageCheck,
        "issue-link": IssueLinkCheck,
        "malicious-prompt": MaliciousPromptCheck,
        "security": SecurityCheck,
        "security-context": SecurityContextCheck,
    }


def run_enabled_checks(
    pr: PullRequest,
    diff: str,
    project_config: ProjectConfig,
    operator_config: OperatorConfig | None = None,
) -> list[ReviewDraft]:
    """Run all LLM checks enabled in project config and return resulting drafts.

    Each check builds a focused prompt, calls the first configured LLM backend,
    parses findings, and feeds them through ``create_drafts_from_findings`` for
    anti-pattern gating and persistence.
    """
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.config.models import OperatorConfig as DefaultOperatorConfig

    if operator_config is None:
        operator_config = DefaultOperatorConfig()

    enabled = project_config.llm_checks
    if not enabled:
        return []

    registry = _get_registry()
    pr_context = build_pr_context(pr, project_config, operator_config)

    if "issue-link" in enabled and not pr_context.linked_issues_context:
        from franktheunicorn.review.drafter import fetch_linked_issues_context

        pr_context.linked_issues_context = fetch_linked_issues_context(pr)

    # Use first configured backend (or stub fallback).
    backend_configs = operator_config.llm_backends or [LLMBackendConfig()]
    backend_config = backend_configs[0]

    all_drafts: list[ReviewDraft] = []

    for check_name in enabled:
        check_cls = registry.get(check_name)
        if check_cls is None:
            logger.warning("Unknown LLM check '%s'; skipping.", check_name)
            continue

        check = check_cls()
        try:
            findings = _run_check_dispatch(check, pr, diff, pr_context, backend_config)
        except Exception:
            logger.exception("LLM check '%s' failed.", check_name)
            continue

        if not findings:
            continue

        source = f"check:{check_name}"
        drafts = create_drafts_from_findings(
            pr,
            findings,
            source=source,
            project=pr.project,
        )
        all_drafts.extend(drafts)

    return all_drafts


def _run_check_dispatch(
    check: BaseCheck,
    pr: PullRequest,
    diff: str,
    pr_context: PRContext,
    backend_config: LLMBackendConfig,
) -> list[ReviewFinding]:
    """Dispatch to a check's custom ``scan(pr, diff, backend_config)`` method
    if it defines one; otherwise run the standard prompt-based pipeline."""
    scan = getattr(check, "scan", None)
    if callable(scan):
        return list(scan(pr, diff, backend_config))

    return _run_single_check(check, diff, pr_context, backend_config)


def _run_single_check(
    check: BaseCheck,
    diff: str,
    pr_context: PRContext,
    backend_config: LLMBackendConfig,
) -> list[ReviewFinding]:
    """Run one check against a single LLM backend.

    For backends that extend ``BaseLLMBackend`` we call ``_call_api`` directly
    with the check's custom prompt.  For other backends (e.g. StubBackend) we
    fall back to ``generate_findings`` which uses the default review prompt —
    the findings still flow through the check pipeline.
    """
    from franktheunicorn.review.backends import get_backend
    from franktheunicorn.review.backends.base import BaseLLMBackend

    backend = get_backend(backend_config)

    if isinstance(backend, BaseLLMBackend):
        system_prompt, user_message = check.build_prompt(diff, pr_context)
        api_key = backend._resolve_api_key()
        raw_text = backend._call_api(system_prompt, user_message, api_key)
        return check.parse_response(raw_text)

    # Fallback for backends without _call_api (e.g. StubBackend).
    return backend.generate_findings(diff, pr_context)
