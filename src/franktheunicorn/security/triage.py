"""Security report triage pipeline.

Uses existing LLM backends to parse and analyze security reports,
then checks CVE databases for duplicates.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from franktheunicorn.security.cve_lookup import search_cves
from franktheunicorn.security.prompt import build_parse_prompt, build_triage_prompt

_VALID_SEVERITIES: frozenset[str] = frozenset(
    {"critical", "high", "medium", "low", "informational"}
)

# Statuses that can be overwritten by automatic triage analysis.
# Operator-set statuses (valid, invalid, duplicate) are preserved on re-triage.
_AUTO_MANAGED_STATUSES: frozenset[str] = frozenset({"new", "triaging"})

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import SecurityReport
    from franktheunicorn.review.backends.base import BaseLLMBackend

logger = logging.getLogger(__name__)


def triage_report(
    report: SecurityReport,
    project_config: ProjectConfig | None,
    operator_config: OperatorConfig,
) -> SecurityReport:
    """Run full triage pipeline on a security report.

    1. Parse raw text into structured fields via LLM
    2. Load project context (README, docs) if available
    3. Triage: assess POC validity, check for expected behavior
    4. Search CVE database for duplicates
    5. Save results to the report
    """
    logger.info("Starting triage for security report #%d", report.pk)

    # Resolve the backend *before* mutating status — otherwise a deployment
    # with no LLM backends (e.g. email auto-triage on but llm_backends empty)
    # strands every report in "triaging" and it drops out of the "new" queue.
    backend = _get_triage_backend(operator_config)
    if backend is None:
        logger.warning("No LLM backend configured; skipping triage for report #%d.", report.pk)
        return report

    # Only claim the status when it's ours to claim. A report the operator
    # already ruled on (valid / invalid / duplicate) keeps that verdict through
    # a re-triage — overwriting it with "triaging" here is what made
    # _analyze_report's _AUTO_MANAGED_STATUSES guard unable to protect it.
    if report.status in _AUTO_MANAGED_STATUSES:
        report.status = "triaging"
        report.save(update_fields=["status", "updated_at"])

    # Whatever happens below, the report must not be left sitting in
    # "triaging": that status is invisible in the "new" queue, so a model that
    # times out or answers with unparseable JSON would silently swallow the
    # report. _analyze_report moves it off "triaging" on success; this puts it
    # back in the queue on every other path.
    try:
        logger.info("Parsing report #%d via LLM", report.pk)
        _parse_report(report, backend)
        logger.info(
            "Parse complete for report #%d: severity=%r component=%r",
            report.pk,
            report.assessed_severity,
            report.parsed_component[:60] if report.parsed_component else "",
        )

        project_context = _load_project_context(report, project_config)
        # CVE lookup runs before analysis so the matches are available as
        # context for the expected-behavior / duplicate call.
        _check_cves(report, operator_config)
        security_model = _resolve_security_model(project_config)

        from franktheunicorn.security.learning import resolve_triage_guidance

        learned_guidance = resolve_triage_guidance(report.project)
        logger.info("Analyzing report #%d via LLM", report.pk)
        _analyze_report(
            report,
            backend,
            project_context,
            security_model=security_model,
            cve_candidates=report.cve_matches,
            learned_guidance=learned_guidance,
        )
    finally:
        _restore_untriaged_status(report)

    logger.info(
        "Triage complete for report #%d: severity=%r status=%r poc_plausible=%s",
        report.pk,
        report.assessed_severity,
        report.status,
        report.poc_plausible,
    )

    return report


def _restore_untriaged_status(report: SecurityReport) -> None:
    """Return a report still marked ``triaging`` to the ``new`` queue.

    Runs from a ``finally``, so it swallows its own errors: raising here would
    replace whatever exception was already on its way out with a much less
    informative one.
    """
    if report.status != "triaging":
        return
    try:
        report.status = "new"
        report.save(update_fields=["status", "updated_at"])
    except Exception:
        logger.exception("Could not return report #%d to the new queue", report.pk)
        return
    logger.warning(
        "Triage produced no verdict for report #%d; returned it to the new queue.",
        report.pk,
    )


def _get_triage_backend(operator_config: OperatorConfig) -> BaseLLMBackend | None:
    """Get the first configured LLM backend for triage."""
    if not operator_config.llm_backends:
        return None

    from franktheunicorn.review.backends import get_backend
    from franktheunicorn.review.backends.base import BaseLLMBackend

    backend = get_backend(operator_config.llm_backends[0])
    if not isinstance(backend, BaseLLMBackend):
        return None
    return backend


def _call_llm(
    backend: BaseLLMBackend,
    system_prompt: str,
    user_message: str,
    *,
    action_type: str,
    project_id: int | None = None,
) -> dict[str, object] | None:
    """Call the LLM backend and parse JSON response. Returns None on failure.

    Goes through the backend's metered-call path so the triage call's token
    usage is recorded as a CostRecord (previously each caller recorded cost
    separately, which the raw ``_call_api`` bypass silently skipped).
    """
    raw_response = backend.metered_call(
        system_prompt,
        user_message,
        action_type=action_type,
        project_id=project_id,
    )
    return _safe_json_parse(raw_response)


def _coerce_bool(value: object) -> bool:
    """Coerce an LLM JSON value to bool, handling string 'true'/'false'."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)


def _parse_report(report: SecurityReport, backend: BaseLLMBackend) -> None:
    """Parse raw report text into structured fields via LLM."""
    system_prompt, user_message = build_parse_prompt(report.raw_text)
    project_id = report.project_id if report.project else None

    try:
        parsed = _call_llm(
            backend,
            system_prompt,
            user_message,
            action_type="security-parse",
            project_id=project_id,
        )
    except Exception:
        logger.exception("Failed to parse security report %d", report.pk)
        return

    if parsed:
        report.title = report.title or str(parsed.get("title", ""))[:500]
        report.parsed_component = str(parsed.get("component", ""))[:500]
        report.parsed_poc = str(parsed.get("poc", ""))
        report.parsed_impact = str(parsed.get("impact", ""))
        severity = str(parsed.get("severity", "unknown")).lower()
        report.assessed_severity = severity if severity in _VALID_SEVERITIES else "unknown"

        if not report.reporter_name and parsed.get("reporter_name"):
            report.reporter_name = str(parsed["reporter_name"])[:255]
        if not report.reporter_email and parsed.get("reporter_email"):
            report.reporter_email = str(parsed["reporter_email"])[:255]

        report.save(
            update_fields=[
                "title",
                "parsed_component",
                "parsed_poc",
                "parsed_impact",
                "assessed_severity",
                "reporter_name",
                "reporter_email",
                "updated_at",
            ]
        )


def _analyze_report(
    report: SecurityReport,
    backend: BaseLLMBackend,
    project_context: str,
    security_model: str = "",
    cve_candidates: list[object] | None = None,
    learned_guidance: str = "",
) -> None:
    """Run triage analysis on parsed report."""
    system_prompt, user_message = build_triage_prompt(
        parsed_component=report.parsed_component,
        parsed_poc=report.parsed_poc,
        parsed_impact=report.parsed_impact,
        project_context=project_context,
        security_model=security_model,
        cve_candidates=cve_candidates,
        learned_guidance=learned_guidance,
    )

    project_id = report.project_id if report.project else None

    try:
        analysis = _call_llm(
            backend,
            system_prompt,
            user_message,
            action_type="security-triage",
            project_id=project_id,
        )
    except Exception:
        logger.exception("Failed to analyze security report %d", report.pk)
        return

    if analysis:
        report.poc_plausible = _coerce_bool(analysis.get("poc_plausible", False))
        report.poc_assessment = str(analysis.get("poc_assessment", ""))
        report.is_expected_behavior = _coerce_bool(analysis.get("is_expected_behavior", False))
        report.expected_behavior_explanation = str(
            analysis.get("expected_behavior_explanation", "")
        )
        report.triage_summary = str(analysis.get("triage_summary", ""))

        severity = str(analysis.get("assessed_severity", "")).lower()
        if severity in _VALID_SEVERITIES:
            report.assessed_severity = severity

        # Only auto-set status if operator hasn't already set a manual verdict.
        if report.status in _AUTO_MANAGED_STATUSES:
            report.status = "expected-behavior" if report.is_expected_behavior else "new"

        report.save(
            update_fields=[
                "poc_plausible",
                "poc_assessment",
                "is_expected_behavior",
                "expected_behavior_explanation",
                "triage_summary",
                "assessed_severity",
                "status",
                "updated_at",
            ]
        )


def _check_cves(report: SecurityReport, operator_config: OperatorConfig) -> None:
    """Search NVD for matching CVEs."""
    keyword = report.parsed_component or report.title
    if not keyword:
        logger.debug("Skipping CVE lookup for report #%d: no keyword available", report.pk)
        return

    logger.info("CVE lookup for report #%d (keyword=%r)", report.pk, keyword[:80])
    api_key_env = operator_config.security_triage.nvd_api_key_env
    matches = search_cves(keyword, api_key_env=api_key_env)
    logger.info("CVE lookup complete for report #%d: %d match(es)", report.pk, len(matches))

    # Always save results (even empty) so stale matches are cleared on re-run.
    report.cve_matches = [m.to_dict() for m in matches]
    report.save(update_fields=["cve_matches", "updated_at"])


def _read_file(path: Path, max_chars: int = 5000) -> str | None:
    """Read a file's text content, returning None on failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError:
        logger.debug("Failed to read %s", path, exc_info=True)
        return None


# Conventional in-repo threat-model files, in priority order. SECURITY.md is
# deliberately excluded — by convention it is a vulnerability-reporting policy,
# not a statement of the project's trust boundaries.
_SECURITY_MODEL_FILENAMES: tuple[str, ...] = (
    ".frank/security-model.md",
    "SECURITY_MODEL.md",
    "SECURITY-MODEL.md",
    "THREAT_MODEL.md",
    "docs/security-model.md",
    "docs/threat-model.md",
)


def _resolve_repo_path(project_config: ProjectConfig | None) -> Path | None:
    """Return the checked-out repo directory for a project, or None.

    None when there is no project config, no configured repos dir, or the repo
    has not been cloned yet.
    """
    if project_config is None:
        return None

    from django.conf import settings

    repos_dir_str = getattr(settings, "FRANK_REPOS_DIR", "")
    if not repos_dir_str:
        return None

    repo_path = Path(repos_dir_str) / project_config.owner / project_config.repo
    return repo_path if repo_path.is_dir() else None


def _resolve_security_model(project_config: ProjectConfig | None) -> str:
    """Resolve the project's security model (trust boundaries) for triage.

    Precedence:
      1. Inline ``security_model`` prose in the project YAML (explicit override).
      2. An explicit ``security_model_file`` path, loaded from the repo.
      3. A conventional threat-model file auto-discovered in the repo.
      4. Empty string (triage falls back to README/SECURITY.md context only).

    Files are read fresh from the checked-out base repo each time (no cache),
    and paths are constrained to inside the repo directory.
    """
    if project_config is None:
        return ""

    inline = project_config.security_model.strip()
    if inline:
        return inline

    repo_path = _resolve_repo_path(project_config)
    if repo_path is None:
        return ""

    explicit = project_config.security_model_file.strip()
    if explicit:
        # Explicit path wins over auto-discovery. Constrain it to the repo.
        text = _read_repo_file(repo_path, explicit)
        return text.strip() if text else ""

    for name in _SECURITY_MODEL_FILENAMES:
        text = _read_repo_file(repo_path, name)
        if text and text.strip():
            return text.strip()
    return ""


def _read_repo_file(repo_path: Path, relative: str, max_chars: int = 8000) -> str | None:
    """Read a repo-relative file, refusing to escape the repo directory."""
    candidate = (repo_path / relative).resolve()
    if not candidate.is_relative_to(repo_path.resolve()):
        return None
    if not candidate.is_file():
        return None
    return _read_file(candidate, max_chars=max_chars)


def _load_project_context(
    report: SecurityReport,
    project_config: ProjectConfig | None,
) -> str:
    """Load project README and docs for triage context."""
    if report.project is None:
        return ""

    repo_path = _resolve_repo_path(project_config)
    if repo_path is None:
        return ""

    parts: list[str] = []

    # Read first available README variant.
    for name in ("README.md", "README.rst", "README.txt", "README"):
        readme = repo_path / name
        if readme.is_file():
            text = _read_file(readme)
            if text:
                parts.append(f"### README\n{text}")
            break

    # Read SECURITY.md if present (usually the reporting policy).
    text = _read_file(repo_path / "SECURITY.md", max_chars=3000)
    if text:
        parts.append(f"### SECURITY.md\n{text}")

    # Read a security guidance doc if present. Many projects keep their actual
    # security posture here rather than in SECURITY.md — Apache Spark, for
    # example, documents auth/encryption/trust boundaries in docs/security.md.
    # This is supporting context, separate from the authoritative
    # trust-boundary `security_model`.
    for name in ("docs/security.md", "docs/security.rst", "docs/SECURITY.md"):
        text = _read_repo_file(repo_path, name, max_chars=4000)
        if text and text.strip():
            parts.append(f"### {name}\n{text}")
            break

    # Read the reported component file if identifiable and safe.
    if report.parsed_component:
        text = _read_repo_file(repo_path, report.parsed_component, max_chars=5000)
        if text:
            parts.append(f"### Source: {report.parsed_component}\n{text}")

    return "\n\n".join(parts)


def _safe_json_parse(raw_text: str) -> dict[str, object] | None:
    """Parse JSON from LLM response, stripping code fences if present."""
    from franktheunicorn.review.backends.base import _CODE_FENCE_RE

    raw_text = raw_text.strip()
    if not raw_text:
        return None

    fence_match = _CODE_FENCE_RE.search(raw_text)
    if fence_match:
        raw_text = fence_match.group(1)

    try:
        data = json.loads(raw_text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        logger.warning("LLM response is not valid JSON for security triage.")
    return None
