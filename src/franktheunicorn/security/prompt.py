"""Prompt construction for security report triage."""

from __future__ import annotations


def build_parse_prompt(raw_text: str) -> tuple[str, str]:
    """Build system + user prompts to parse a raw security report.

    Returns (system_prompt, user_message).
    """
    system_prompt = (
        "You are a security report parser. Extract structured fields from the "
        "raw security report text provided. Return a JSON object with exactly "
        "these keys:\n"
        '  "title": short summary of the reported issue,\n'
        '  "component": the affected component/module/file,\n'
        '  "poc": the proof-of-concept steps (verbatim from the report),\n'
        '  "impact": the claimed impact or consequence,\n'
        '  "severity": one of "critical", "high", "medium", "low", "informational",\n'
        '  "reporter_name": reporter name if present (empty string if not),\n'
        '  "reporter_email": reporter email if present (empty string if not).\n'
        "Return ONLY the JSON object, no markdown fences or extra text."
    )

    user_message = f"Parse this security report:\n\n{raw_text}"
    return system_prompt, user_message


def build_triage_prompt(
    parsed_component: str,
    parsed_poc: str,
    parsed_impact: str,
    project_context: str,
    security_model: str = "",
    cve_candidates: list[object] | None = None,
) -> tuple[str, str]:
    """Build system + user prompts for triage analysis.

    ``project_context`` should include README excerpts, relevant docs,
    or code snippets that describe expected behavior.

    ``security_model`` is the project's stated threat model / trust
    boundaries (from ``ProjectConfig.security_model``). When provided, it is
    the primary lens for the expected-behavior call.

    ``cve_candidates`` are CVE records from a keyword search (may be
    unrelated); they help spot reports that duplicate a known/already-fixed
    issue.

    Returns (system_prompt, user_message).
    """
    system_prompt = (
        "You are a security triage analyst for an open-source project. "
        "Analyze the reported vulnerability against the project context and, "
        "above all, the project's stated security model.\n\n"
        "Many reports describe expected/documented behavior (e.g. a tool that "
        "runs shell commands is reported for 'running shell commands'). Weigh "
        "the project's security model heavily: if it explicitly treats an "
        "input as trusted (e.g. submitted code, loaded models/pipelines, "
        "cluster job payloads that are documented to run arbitrary code), then "
        "code execution reached only through that trusted input is expected "
        "behavior, not a finding — even when the reporter frames it as a "
        "critical vulnerability.\n\n"
        "Pay attention to the *input channel* the exploit travels through. "
        "Code or file-system access reachable only by someone who can already "
        "submit code, models, or jobs is usually expected. The same primitive "
        "reached by feeding a normally-untrusted artifact — a data file read "
        "by the engine, an HTTP request parameter, a rendered web-UI value, a "
        "network message — is usually a real finding. Do not wave a report "
        "away as 'we already run arbitrary code' when the trigger is an "
        "ordinary data file or an unauthenticated request.\n\n"
        "If candidate CVE matches are provided, judge their relevance and note "
        "in the summary whether the report appears to duplicate a known or "
        "already-fixed issue.\n\n"
        "Return a JSON object with exactly these keys:\n"
        '  "poc_plausible": boolean — does the POC demonstrate a real vulnerability?,\n'
        '  "poc_assessment": string — detailed assessment of the POC,\n'
        '  "is_expected_behavior": boolean — is the reported behavior documented/expected '
        "under the project's security model?,\n"
        '  "expected_behavior_explanation": string — explain why (empty if not expected),\n'
        '  "assessed_severity": one of "critical", "high", "medium", "low", '
        '"informational",\n'
        '  "triage_summary": string — concise triage summary for the maintainer, noting any '
        "likely duplicate CVE.\n"
        "Return ONLY the JSON object, no markdown fences or extra text."
    )

    parts = [
        "## Reported Vulnerability\n",
        f"**Component:** {parsed_component}\n",
        f"**POC Steps:**\n{parsed_poc}\n",
        f"**Claimed Impact:** {parsed_impact}\n",
    ]

    if security_model:
        parts.append(
            "\n## Project Security Model / Trust Boundaries\n"
            "This is the project's documented stance on what is trusted. Treat "
            "it as authoritative when deciding whether the report is expected "
            f"behavior:\n{security_model}\n"
        )

    if project_context:
        parts.append(f"\n## Project Context\n{project_context}\n")
    elif not security_model:
        parts.append(
            "\n## Project Context\nNo project documentation available. "
            "Assess based on the report alone.\n"
        )

    cve_block = _format_cve_candidates(cve_candidates)
    if cve_block:
        parts.append(cve_block)

    user_message = "\n".join(parts)
    return system_prompt, user_message


def _format_cve_candidates(cve_candidates: list[object] | None) -> str:
    """Render candidate CVE matches for the triage prompt, or "" if none.

    These come from a keyword search and may be unrelated, so the heading
    tells the model to judge relevance rather than assume a duplicate.
    """
    if not cve_candidates:
        return ""

    lines = [
        "\n## Candidate CVE Matches (from keyword search — may be unrelated)\n"
        "Judge whether any of these actually describe the reported issue:\n"
    ]
    for match in cve_candidates[:10]:
        if not isinstance(match, dict):
            continue
        cve_id = str(match.get("cve_id", "")).strip() or "(unknown id)"
        description = str(match.get("description", "")).strip()
        score = match.get("cvss_score")
        status = str(match.get("status", "")).strip()
        meta = ", ".join(
            part
            for part in (
                f"CVSS {score}" if isinstance(score, (int, float)) else "",
                status,
            )
            if part
        )
        suffix = f" ({meta})" if meta else ""
        lines.append(f"- **{cve_id}**{suffix}: {description}")
    return "\n".join(lines) + "\n"
