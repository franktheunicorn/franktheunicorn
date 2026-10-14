"""What happens to a security report the moment it exists, whatever door it came in.

Three doors create reports (the email poll, the paste form, the zip import)
and until now none of them looked at the text. The regex injection scanner in
``security.malicious_prompt`` was run twice, late, and by the two consumers
that feed the report to an agent with tool access (the verifier, the fix
agent), each keeping the result to itself. So an operator reading the report
page before clicking Verify saw nothing, and every other LLM call that
re-reads the stored text (parse, triage, dedup, recheck) ran blind.

Magpie's import skill runs the check at import and persists a callout above
the body "so the marker persists for every future skill invocation"
(``plugins/magpie-security/skills/issue-import/apply.md`` at apache/magpie
commit dadb315caa8b881fc1d2195bef03223dac25915a). This is frank's version:
one function the doors call after the row is created and triage calls again
once the POC field is populated (new text), writing ``injection_hits`` on the
row. Recording the hit is the mitigation: nothing is refused here. The
verifier keeps its own refuse-on-injection gate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franktheunicorn.core.models import SecurityReport

logger = logging.getLogger(__name__)


def injection_hits_for(report: SecurityReport) -> list[str]:
    """Prompt-injection patterns in the report's text, by name. Empty is clean.

    Title, raw text and the parsed POC: the same text the verifier scans, so
    the two never disagree about the same report.
    """
    from franktheunicorn.security.malicious_prompt import regex_scan

    text = f"{report.title}\n{report.raw_text}\n{report.parsed_poc}"
    return sorted({hit.pattern_name for hit in regex_scan(text)})


def record_injection_hits(report: SecurityReport) -> list[str]:
    """Scan *report* and persist the result on ``injection_hits``.

    Saves only when the value changed, so a re-scan after triage of a clean
    report is one SELECT and no write. Logs at WARNING when there are hits,
    naming the patterns: a steering-shaped report is the sharpest input this
    codebase handles and the log is where the operator looks when a later
    verdict seems off.
    """
    hits = injection_hits_for(report)
    if hits != list(report.injection_hits):
        report.injection_hits = hits
        report.save(update_fields=["injection_hits", "updated_at"])
    if hits:
        logger.warning(
            "Security report #%s contains prompt-injection patterns (%s). Recorded on the "
            "report; every prompt that carries its text frames it as data, and the verifier's "
            "refuse_on_injection gate decides whether an agent sees it at all.",
            report.pk,
            ", ".join(hits),
        )
    return hits
