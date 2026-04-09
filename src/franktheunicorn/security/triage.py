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

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import SecurityReport

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
    report.status = "triaging"
    report.save(update_fields=["status", "updated_at"])

    backend = _get_triage_backend(operator_config)
    if backend is None:
        logger.warning("No LLM backend configured; skipping triage.")
        return report

    # Step 1: Parse the raw report.
    _parse_report(report, backend, operator_config)

    # Step 2: Load project context.
    project_context = _load_project_context(report, project_config)

    # Step 3: Triage analysis.
    _analyze_report(report, backend, project_context, operator_config)

    # Step 4: CVE dedup.
    _check_cves(report, operator_config)

    return report


def _get_triage_backend(operator_config: OperatorConfig) -> object | None:
    """Get the first configured LLM backend for triage."""
    if not operator_config.llm_backends:
        return None

    from franktheunicorn.review.backends import get_backend

    return get_backend(operator_config.llm_backends[0])


def _parse_report(
    report: SecurityReport,
    backend: object,
    operator_config: OperatorConfig,
) -> None:
    """Parse raw report text into structured fields via LLM."""
    from franktheunicorn.review.backends.base import BaseLLMBackend

    if not isinstance(backend, BaseLLMBackend):
        return

    system_prompt, user_message = build_parse_prompt(report.raw_text)
    api_key = backend._resolve_api_key()

    try:
        raw_response = backend._call_api(system_prompt, user_message, api_key)
        parsed = _safe_json_parse(raw_response)
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

    # Record cost.
    project_id = report.project_id if report.project else None
    backend.record_cost(project_id, None, action_type="security-parse")


def _analyze_report(
    report: SecurityReport,
    backend: object,
    project_context: str,
    operator_config: OperatorConfig,
) -> None:
    """Run triage analysis on parsed report."""
    from franktheunicorn.review.backends.base import BaseLLMBackend

    if not isinstance(backend, BaseLLMBackend):
        return

    system_prompt, user_message = build_triage_prompt(
        parsed_component=report.parsed_component,
        parsed_poc=report.parsed_poc,
        parsed_impact=report.parsed_impact,
        project_context=project_context,
    )
    api_key = backend._resolve_api_key()

    try:
        raw_response = backend._call_api(system_prompt, user_message, api_key)
        analysis = _safe_json_parse(raw_response)
    except Exception:
        logger.exception("Failed to analyze security report %d", report.pk)
        return

    if analysis:
        report.poc_plausible = bool(analysis.get("poc_plausible", False))
        report.poc_assessment = str(analysis.get("poc_assessment", ""))
        report.is_expected_behavior = bool(analysis.get("is_expected_behavior", False))
        report.expected_behavior_explanation = str(
            analysis.get("expected_behavior_explanation", "")
        )
        report.triage_summary = str(analysis.get("triage_summary", ""))

        severity = str(analysis.get("assessed_severity", "")).lower()
        if severity in _VALID_SEVERITIES:
            report.assessed_severity = severity

        # Auto-set status based on analysis.
        if report.is_expected_behavior:
            report.status = "expected-behavior"
        else:
            report.status = "new"  # needs operator review regardless of POC assessment

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

    project_id = report.project_id if report.project else None
    backend.record_cost(project_id, None, action_type="security-triage")


def _check_cves(report: SecurityReport, operator_config: OperatorConfig) -> None:
    """Search NVD for matching CVEs."""
    keyword = report.parsed_component or report.title
    if not keyword:
        return

    api_key_env = operator_config.security_triage.nvd_api_key_env
    matches = search_cves(keyword, api_key_env=api_key_env)

    if matches:
        report.cve_matches = [m.to_dict() for m in matches]
        report.save(update_fields=["cve_matches", "updated_at"])


def _load_project_context(
    report: SecurityReport,
    project_config: ProjectConfig | None,
) -> str:
    """Load project README and docs for triage context."""
    if project_config is None or report.project is None:
        return ""

    from django.conf import settings

    repos_dir = Path(getattr(settings, "FRANK_REPOS_DIR", ""))
    repo_path = repos_dir / project_config.owner / project_config.repo

    if not repo_path.is_dir():
        return ""

    parts: list[str] = []

    # Read README.
    for readme_name in ("README.md", "README.rst", "README.txt", "README"):
        readme_path = repo_path / readme_name
        if readme_path.is_file():
            try:
                text = readme_path.read_text(encoding="utf-8", errors="replace")
                parts.append(f"### README\n{text[:5000]}")
            except OSError:
                pass
            break

    # Read SECURITY.md if present.
    security_md = repo_path / "SECURITY.md"
    if security_md.is_file():
        try:
            text = security_md.read_text(encoding="utf-8", errors="replace")
            parts.append(f"### SECURITY.md\n{text[:3000]}")
        except OSError:
            pass

    # Read docs about the component if identifiable.
    if report.parsed_component:
        component_path = (repo_path / report.parsed_component).resolve()
        # Guard against path traversal (parsed_component comes from LLM output).
        if component_path.is_relative_to(repo_path) and component_path.is_file():
            try:
                text = component_path.read_text(encoding="utf-8", errors="replace")
                parts.append(f"### Source: {report.parsed_component}\n{text[:5000]}")
            except OSError:
                pass

    return "\n\n".join(parts)


def _safe_json_parse(raw_text: str) -> dict[str, object] | None:
    """Parse JSON from LLM response, stripping code fences if present."""
    from franktheunicorn.review.backends.base import _CODE_FENCE_RE

    raw_text = raw_text.strip()
    if not raw_text:
        return None

    # Strip markdown code fences (reuse regex from review backend).
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
