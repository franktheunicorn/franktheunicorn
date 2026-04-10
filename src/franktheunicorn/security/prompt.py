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
) -> tuple[str, str]:
    """Build system + user prompts for triage analysis.

    ``project_context`` should include README excerpts, relevant docs,
    or code snippets that describe expected behavior.

    Returns (system_prompt, user_message).
    """
    system_prompt = (
        "You are a security triage analyst for an open-source project. "
        "Analyze the reported vulnerability against the project context. "
        "Many reports describe expected/documented behavior (e.g. a tool that "
        "runs shell commands is reported for 'running shell commands'). "
        "Return a JSON object with exactly these keys:\n"
        '  "poc_plausible": boolean — does the POC demonstrate a real vulnerability?,\n'
        '  "poc_assessment": string — detailed assessment of the POC,\n'
        '  "is_expected_behavior": boolean — is the reported behavior documented/expected?,\n'
        '  "expected_behavior_explanation": string — explain why (empty if not expected),\n'
        '  "assessed_severity": one of "critical", "high", "medium", "low", '
        '"informational",\n'
        '  "triage_summary": string — concise triage summary for the maintainer.\n'
        "Return ONLY the JSON object, no markdown fences or extra text."
    )

    parts = [
        "## Reported Vulnerability\n",
        f"**Component:** {parsed_component}\n",
        f"**POC Steps:**\n{parsed_poc}\n",
        f"**Claimed Impact:** {parsed_impact}\n",
    ]
    if project_context:
        parts.append(f"\n## Project Context\n{project_context}\n")
    else:
        parts.append(
            "\n## Project Context\nNo project documentation available. "
            "Assess based on the report alone.\n"
        )

    user_message = "\n".join(parts)
    return system_prompt, user_message
