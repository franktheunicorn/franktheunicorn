"""Prompt construction for security report triage."""

from __future__ import annotations

from franktheunicorn.core.untrusted import (
    UNTRUSTED_CONTENT_RULE,
    fence,
    fence_note,
    fresh_nonce,
)

#: Cap on the report text handed to the parse call, in characters.
#:
#: There was no cap, which was survivable while every report arrived by paste or
#: email and self-limited. A bulk import doesn't: a real scanner archive produced
#: a 380,591-character report, roughly 95k tokens for a call whose entire job is
#: to pull seven short fields out of the top of it. Times 257 reports in one
#: import. Anything this long is a rollup document rather than a single report,
#: and the fields being extracted — title, component, POC, impact — are near the
#: start in every format we've seen. ~30k characters is about 8k tokens, which
#: fits every backend's window with room for the response.
MAX_PARSE_CHARS = 30_000

#: Marker appended when the text is cut, so the model is told rather than left to
#: infer that a report stops mid-sentence.
_TRUNCATION_MARKER = "\n\n[report truncated for length]"


def truncate_for_prompt(raw_text: str, limit: int = MAX_PARSE_CHARS) -> str:
    """Cut *raw_text* to *limit* characters at a line boundary where possible."""
    if len(raw_text) <= limit:
        return raw_text
    cut = raw_text.rfind("\n", 0, limit)
    if cut <= 0:
        cut = limit
    return raw_text[:cut] + _TRUNCATION_MARKER


def build_parse_prompt(raw_text: str, nonce: str = "") -> tuple[str, str]:
    """Build system + user prompts to parse a raw security report.

    Returns (system_prompt, user_message). The report text is capped — see
    ``MAX_PARSE_CHARS`` — and fenced as untrusted data (``core.untrusted``);
    ``nonce`` is for tests.
    """
    nonce = nonce or fresh_nonce()
    system_prompt = (
        "You are a security report parser. Extract structured fields from the "
        "raw security report text provided. Return a JSON object with exactly "
        "these keys:\n"
        '  "title": short summary of the reported issue,\n'
        '  "component": the affected component/module/file,\n'
        '  "poc": the proof-of-concept steps (verbatim from the report),\n'
        '  "impact": the claimed impact or consequence,\n'
        '  "severity": the severity the REPORTER claims, in their words mapped to one of '
        '"critical", "high", "medium", "low", "informational",\n'
        '  "reporter_name": reporter name if present (empty string if not),\n'
        '  "reporter_email": reporter email if present (empty string if not).\n'
        "Return ONLY the JSON object, no markdown fences or extra text.\n\n"
        + UNTRUSTED_CONTENT_RULE
    )

    user_message = (
        "Parse this security report.\n\n"
        + fence_note(nonce)
        + "\n\n"
        + fence("report", truncate_for_prompt(raw_text), nonce)
    )
    return system_prompt, user_message


#: The six triage dispositions, plus the honest "not sure".
#:
#: Adapted from Apache Magpie's ``security-issue-triage`` skill
#: (``plugins/magpie-security/skills/issue-triage/classification.md`` at
#: apache/magpie commit dadb315caa8b881fc1d2195bef03223dac25915a, Apache-2.0).
#: Magpie's point, and the reason frank takes the taxonomy rather than its
#: three-way plausible/expected/invalid split: "fact-correct but outside the
#: security model" (DEFENSE-IN-DEPTH, INFO-ONLY) is the most common shape in a
#: real backlog and it is neither valid nor invalid. The class is a proposal; the
#: operator's status is the verdict. The actor/effect cheat-sheet Magpie ships
#: next to this is Airflow-specific and is deliberately not copied.
TRIAGE_DISPOSITIONS: tuple[str, ...] = (
    "VALID",
    "DEFENSE-IN-DEPTH",
    "INFO-ONLY",
    "INVALID",
    "PROBABLE-DUP",
    "FIX-ALREADY-PUBLIC",
    "UNCERTAIN",
)

TRIAGE_DISPOSITION_RUBRIC = (
    "Classify the report with ONE disposition, and quote the line of the security "
    "model or project context the call rests on:\n"
    "- VALID: the behaviour violates a documented rule of the security model, the "
    "vector is reachable by someone WITHOUT an authoritative role (operator, host "
    "admin, a trusted code/job submitter), and the premise checks out.\n"
    "- DEFENSE-IN-DEPTH: the code does what the report says, but the attack model "
    "falls outside the security model's boundary (a local user on a trusted host, "
    "a multi-tenant setup the project does not support); a hardening change is "
    "still welcome as an ordinary fix, with no CVE.\n"
    "- INFO-ONLY: fact-correct, violates no documented rule, and needs only an "
    "explanation, not a change.\n"
    "- INVALID: the premise is wrong (the code does not do that), the framing is "
    "circular, or the behaviour is documented as by design.\n"
    "- PROBABLE-DUP: the same root cause as a candidate CVE or a known advisory, "
    "or a sibling vector with the same fix shape.\n"
    "- FIX-ALREADY-PUBLIC: a public fix already exists for exactly this, so the "
    "next step is confirming it covers the report rather than triaging it.\n"
    "- UNCERTAIN: the input is too ambiguous to pick one; say what would settle "
    "it. Never dress an uncertain call up as a confident one.\n"
    "A severity is a guess for context, never a score; the reporter's own "
    "severity claim is informational and frequently inflated.\n\n"
)


def build_triage_prompt(
    parsed_component: str,
    parsed_poc: str,
    parsed_impact: str,
    project_context: str,
    security_model: str = "",
    cve_candidates: list[object] | None = None,
    learned_guidance: str = "",
    source_agent: str = "",
    nonce: str = "",
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

    ``learned_guidance`` is the distilled addendum built from prior operator
    agree/disagree feedback (see ``security/learning.py``). When provided,
    it is appended to the system prompt.

    ``source_agent`` names the automated agent that produced the report, when
    one did (``SecurityReport.source_agent``). Its findings fail differently
    from a person's, and the model is told how, so a confidently-worded scanner
    finding isn't graded on its confidence.

    The reported component, POC and impact are the reporter's words and go
    inside a nonce-marked fence (``core.untrusted``); the security model, the
    project context and the CVE candidates are frank's and stay outside it.
    ``nonce`` is for tests.

    Returns (system_prompt, user_message).
    """
    nonce = nonce or fresh_nonce()
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
        "A scenario that only works because authentication (or another "
        "documented security feature) has been disabled or turned off is not "
        "a vulnerability: the feature exists precisely to prevent it, and "
        "running without it is a configuration choice the documentation warns "
        "against. Mark such reports is_expected_behavior=true and say the "
        "report can be closed as not-a-vulnerability. Careful about the "
        "direction: an attack that works *despite* authentication, or one "
        "that itself disables authentication, is a real finding.\n\n"
        "If candidate CVE matches are provided, judge their relevance and note "
        "in the summary whether the report appears to duplicate a known or "
        "already-fixed issue.\n\n"
        + TRIAGE_DISPOSITION_RUBRIC
        + "Return a JSON object with exactly these keys:\n"
        '  "poc_plausible": boolean — does the POC demonstrate a real vulnerability?,\n'
        '  "poc_assessment": string — detailed assessment of the POC,\n'
        '  "is_expected_behavior": boolean — is the reported behavior documented/expected '
        "under the project's security model?,\n"
        '  "expected_behavior_explanation": string — explain why (empty if not expected),\n'
        '  "assessed_severity": one of "critical", "high", "medium", "low", '
        '"informational",\n'
        '  "disposition": one of "VALID", "DEFENSE-IN-DEPTH", "INFO-ONLY", "INVALID", '
        '"PROBABLE-DUP", "FIX-ALREADY-PUBLIC", "UNCERTAIN" — see the rubric above,\n'
        '  "model_quote": string — the one line of the project security model or context, '
        "verbatim, that the disposition rests on; empty when no line applies,\n"
        '  "triage_summary": string — concise triage summary for the maintainer, noting any '
        "likely duplicate CVE.\n"
        "Return ONLY the JSON object, no markdown fences or extra text.\n\n"
        + UNTRUSTED_CONTENT_RULE
    )

    if learned_guidance:
        system_prompt += (
            "\n\nOPERATOR-LEARNED TRIAGE GUIDANCE (from prior agree/disagree "
            f"feedback):\n{learned_guidance}"
        )

    reported = (
        f"**Component:** {parsed_component}\n\n"
        f"**POC Steps:**\n{parsed_poc}\n\n"
        f"**Claimed Impact:** {parsed_impact}\n"
    )
    parts = [
        "## Reported Vulnerability\n",
        fence_note(nonce) + "\n",
        fence("report", reported, nonce) + "\n",
    ]

    if source_agent:
        parts.append(
            "\n## Provenance\n"
            f"This report was produced by an automated agent ({source_agent}), not "
            "written by a person. Machine-generated findings commonly cite files, "
            "functions or call paths that don't exist or don't behave as described, "
            "assert severity without demonstrating impact, and report one root cause "
            "once per call site. Judge the POC and the impact on what is actually "
            "shown; treat the agent's own severity or confidence as its opinion, not "
            "evidence.\n"
        )

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
