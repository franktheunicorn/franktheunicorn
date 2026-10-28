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
from pathlib import Path
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import (
    BaseLLMBackend,
    PRContext,
    ReviewFinding,
    parse_llm_response,
)
from franktheunicorn.review.drafter import build_pr_context, create_drafts_from_findings
from franktheunicorn.review.tone_guard import apply_tone_guard_batch

if TYPE_CHECKING:
    from franktheunicorn.backends.base import ForgeClient
    from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import PullRequest, ReviewDraft
    from franktheunicorn.review.prior_comments import PriorComment

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
    from franktheunicorn.review.checks.api_misuse import APIMisuseCheck
    from franktheunicorn.review.checks.backport import BackportCheck
    from franktheunicorn.review.checks.concurrency import ConcurrencyCheck
    from franktheunicorn.review.checks.config_docs import ConfigDocsCheck
    from franktheunicorn.review.checks.correctness import CorrectnessCheck
    from franktheunicorn.review.checks.coverage import CoverageCheck
    from franktheunicorn.review.checks.cve_class import CVEClassCheck
    from franktheunicorn.review.checks.design import DesignCheck
    from franktheunicorn.review.checks.doc_accuracy import DocAccuracyCheck
    from franktheunicorn.review.checks.edge_case import EdgeCaseCheck
    from franktheunicorn.review.checks.error_framework import ErrorFrameworkCheck
    from franktheunicorn.review.checks.issue_link import IssueLinkCheck
    from franktheunicorn.review.checks.malicious_prompt import MaliciousPromptCheck
    from franktheunicorn.review.checks.parity import ParityCheck
    from franktheunicorn.review.checks.pr_description import PRDescriptionCheck
    from franktheunicorn.review.checks.red_team import RedTeamCheck
    from franktheunicorn.review.checks.security import SecurityCheck
    from franktheunicorn.review.checks.security_context import SecurityContextCheck
    from franktheunicorn.review.checks.security_disclosure import SecurityDisclosureCheck
    from franktheunicorn.review.checks.simplification import SimplificationCheck
    from franktheunicorn.review.checks.test_pinning import TestPinningCheck

    return {
        "api-misuse": APIMisuseCheck,
        "backport": BackportCheck,
        "config-docs": ConfigDocsCheck,
        "concurrency": ConcurrencyCheck,
        "correctness": CorrectnessCheck,
        "coverage": CoverageCheck,
        "cve-class": CVEClassCheck,
        "design": DesignCheck,
        "doc-accuracy": DocAccuracyCheck,
        "edge-case": EdgeCaseCheck,
        "error-framework": ErrorFrameworkCheck,
        "issue-link": IssueLinkCheck,
        "malicious-prompt": MaliciousPromptCheck,
        "parity": ParityCheck,
        "pr-description": PRDescriptionCheck,
        "red-team": RedTeamCheck,
        "security": SecurityCheck,
        "security-context": SecurityContextCheck,
        "security-disclosure": SecurityDisclosureCheck,
        "simplification": SimplificationCheck,
        "test-pinning": TestPinningCheck,
    }


def run_enabled_checks(
    pr: PullRequest,
    diff: str,
    project_config: ProjectConfig,
    operator_config: OperatorConfig | None = None,
    *,
    repo_path: Path | str | None = None,
    forge_client: ForgeClient | None = None,
    prior_comments: list[PriorComment] | None = None,
) -> list[ReviewDraft]:
    """Run all LLM checks enabled in project config and return resulting drafts.

    Each check builds a focused prompt, calls the first configured LLM backend,
    parses findings, and feeds them through ``create_drafts_from_findings`` for
    anti-pattern gating and persistence.

    ``prior_comments`` reaches the checks the same way it reaches the main
    pipeline: into the prompt, and into draft creation so a finding that
    restates an existing comment becomes a ``+1`` or is dropped. The checks
    are a finding source like any other — "security:" restating a reviewer's
    own point is the comment this is meant to stop.
    """
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.config.models import OperatorConfig as DefaultOperatorConfig
    from franktheunicorn.worker.runner import record_agent_run

    if operator_config is None:
        operator_config = DefaultOperatorConfig()

    enabled = project_config.llm_checks
    if not enabled:
        return []

    registry = _get_registry()
    from franktheunicorn.review.prior_comments import format_prior_comments

    # repo_path and diff both reach the context build: an over-cap file now
    # excerpts around its hunks, and the checks were getting neither — no
    # surrounding code at all, on the pass whose whole job is reading around
    # the change.
    pr_context = build_pr_context(
        pr,
        project_config,
        operator_config,
        prior_comments=format_prior_comments(prior_comments or []),
        repo_path=Path(repo_path) if repo_path is not None else None,
        diff=diff,
    )

    if "issue-link" in enabled and not pr_context.linked_issues_context:
        from franktheunicorn.review.drafter import fetch_linked_issues_context

        pr_context.linked_issues_context = fetch_linked_issues_context(pr)

    # One visibility line for a capped run — only when a prompt-based check is
    # enabled (see _cap_diff_for_checks for what caps and what must not).
    if any(not hasattr(registry.get(name), "scan") for name in enabled if name in registry):
        capped = _cap_diff_for_checks(diff, pr_context)
        if len(capped) < len(diff):
            logger.info(
                "LLM checks see a truncated diff: %d of %d chars "
                "(prompt capped at ~%d with context).",
                len(capped),
                len(diff),
                _MAX_CHECK_PROMPT_CHARS,
            )

    # Use first configured backend (or stub fallback).
    backend_configs = operator_config.llm_backends or [LLMBackendConfig()]
    backend_config = backend_configs[0]

    all_drafts: list[ReviewDraft] = []

    for check_name in enabled:
        check_cls = registry.get(check_name)
        if check_cls is None:
            logger.warning("Unknown LLM check '%s'; skipping.", check_name)
            continue

        source = f"check:{check_name}"
        check = _instantiate_check(check_cls, check_name, project_config, repo_path, forge_client)
        try:
            findings = _run_check_dispatch(check, pr, diff, pr_context, backend_config)
        except Exception:
            logger.exception("LLM check '%s' failed.", check_name)
            record_agent_run(pr, source, status="failed")
            continue

        # Recorded even at zero findings, or a clean check reads as "not run".
        record_agent_run(pr, source, status="ok", findings=len(findings))
        if not findings:
            continue

        # The sub-checks are a finding source like the main pipeline, so their
        # findings get the same tone-guard rewrite before persistence — the main
        # path (drafter) runs it and these must not bypass it. Honours the
        # project's tone_guard_enabled flag; batch is 1:1 over the list.
        if (
            getattr(project_config, "tone_guard_enabled", True)
            and backend_config.provider != "stub"
        ):
            findings, _ = apply_tone_guard_batch(
                findings,
                pr_context,
                backend_config=backend_config,
                is_new_contributor=getattr(pr, "is_new_contributor", False),
                new_contributor_addendum=getattr(project_config, "new_contributor_addendum", ""),
                project_id=pr.project_id,
                pr_id=pr.pk,
            )

        drafts = create_drafts_from_findings(
            pr,
            findings,
            source=source,
            project=pr.project,
            diff=diff,
            governance=project_config.governance,
            rlm_scoring=getattr(project_config, "rlm_scoring", None),
            prior_comments=prior_comments,
            operator=operator_config.github_username,
        )
        all_drafts.extend(drafts)

    return all_drafts


def _instantiate_check(
    check_cls: type[BaseCheck],
    check_name: str,
    project_config: ProjectConfig,
    repo_path: Path | str | None,
    forge_client: ForgeClient | None = None,
) -> BaseCheck:
    """Construct a check, passing config kwargs the class accepts."""
    if check_name == "api-misuse":
        from franktheunicorn.review.checks.api_misuse import APIMisuseCheck

        return APIMisuseCheck(
            config=project_config.api_misuse,
            package_roots=project_config.context.package_roots,
            repo_path=repo_path,
        )
    if check_name == "backport":
        from franktheunicorn.review.checks.backport import BackportCheck

        return BackportCheck(
            config=project_config.backport,
            forge_client=forge_client,
        )
    if check_name == "security-disclosure":
        from franktheunicorn.review.checks.security_disclosure import SecurityDisclosureCheck

        return SecurityDisclosureCheck(governance=project_config.governance)
    if check_name == "cve-class":
        from franktheunicorn.review.checks.cve_class import CVEClassCheck
        from franktheunicorn.security.cve_class_catalog import resolve_cve_class_catalog

        return CVEClassCheck(catalog=resolve_cve_class_catalog(project_config))
    return check_cls()


#: Total prompt size a check may build, in chars (~4 chars per token). The
#: checks all run on the FIRST configured backend; a monster diff made the
#: prompt unanswerable and the response came back empty — see
#: :func:`_metered_call_with_retry` for the other half of that fix.
_MAX_CHECK_PROMPT_CHARS = 120_000

#: The diff always keeps at least this much room, even when the built context
#: is large: the diff is the check's primary input.
_MIN_DIFF_ROOM_CHARS = 20_000


def _cap_diff_for_checks(diff: str, pr_context: PRContext) -> str:
    """Truncate the diff so a prompt-based check's prompt stays bounded, with a
    marker.

    Bounded unless the built context alone exceeds the cap (the diff then keeps
    only its floor) — in that residual case the empty-completion retry in
    :func:`_metered_call_with_retry` fails loudly rather than silently. The
    marker tells the model what it did not see, so a clean result on a
    truncated diff cannot read as "the rest of the PR was checked".

    Only the PROMPT path may use the capped diff: ``scan()`` checks (one reads
    the diff as a security pre-filter) and draft creation (which re-parses the
    diff per finding) always receive the full diff.
    """
    room = (
        _MAX_CHECK_PROMPT_CHARS
        - len(pr_context.full_file_context or "")
        - len(pr_context.imported_modules_context or "")
        - len(pr_context.prior_comments or "")
        - 8_000  # system prompts, PR header, fences, body preview
    )
    room = max(room, _MIN_DIFF_ROOM_CHARS)
    if len(diff) <= room:
        return diff
    cutoff = diff.rfind("\n", 0, room)
    if cutoff <= 0:
        cutoff = room
    else:
        cutoff += 1  # keep the newline the cut lands on
    return (
        diff[:cutoff] + "\n[...diff truncated: the checks reviewed the first part; "
        "the agent CLI reviewers see the full diff in the checkout...]"
    )


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

    return _run_single_check(check, pr, diff, pr_context, backend_config)


def _run_single_check(
    check: BaseCheck,
    pr: PullRequest,
    diff: str,
    pr_context: PRContext,
    backend_config: LLMBackendConfig,
) -> list[ReviewFinding]:
    """Run one check against a single LLM backend.

    For backends that extend ``BaseLLMBackend`` we call the metered LLM path
    with the check's custom prompt (so the call's cost is recorded).  For
    other backends (e.g. StubBackend) we fall back to ``generate_findings``
    which uses the default review prompt — the findings still flow through
    the check pipeline.
    """
    from franktheunicorn.review.backends import get_backend

    backend = get_backend(backend_config)

    if isinstance(backend, BaseLLMBackend):
        # The prompt path caps the diff (see _cap_diff_for_checks).
        diff = _cap_diff_for_checks(diff, pr_context)
        system_prompt, user_message = check.build_prompt(diff, pr_context)
        raw_text = _metered_call_with_retry(
            backend,
            backend_config,
            system_prompt,
            user_message,
            check_name=check.name,
            pr=pr,
        )
        return check.parse_response(raw_text)

    # Fallback for backends without _call_api (e.g. StubBackend).
    return backend.generate_findings(diff, pr_context)


def _metered_call_with_retry(
    backend: BaseLLMBackend,
    backend_config: LLMBackendConfig,
    system_prompt: str,
    user_message: str,
    *,
    check_name: str,
    pr: PullRequest,
) -> str:
    """metered_call + one retry when the completion comes back empty.

    A 200 with empty content is real for the current first backend: qwen3.8
    served with thinking mode can spend the whole output budget on reasoning
    and return no content, and it is intermittent on the same prompt. An
    empty response parsed to zero findings recorded a *clean* check — a check
    that never answered read as "the PR is fine", so it gets one retry and
    then a loud failure instead.
    """
    raw = backend.metered_call(
        system_prompt,
        user_message,
        action_type=f"check:{check_name}",
        project_id=pr.project_id,
        pr_id=pr.pk,
    )
    if raw.strip():
        return raw
    logger.warning(
        "LLM check '%s' got an empty completion from %s/%s (%d-char prompt); retrying once.",
        check_name,
        backend_config.provider,
        backend_config.model,
        len(system_prompt) + len(user_message),
    )
    raw = backend.metered_call(
        system_prompt,
        user_message,
        action_type=f"check:{check_name}",
        project_id=pr.project_id,
        pr_id=pr.pk,
    )
    if raw.strip():
        return raw
    msg = f"backend returned an empty completion for check '{check_name}'"
    raise RuntimeError(msg)
