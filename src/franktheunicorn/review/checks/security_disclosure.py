"""Security-disclosure sub-check: does this public PR say it fixes a vulnerability?

The ``security`` check reads the code for holes the change introduces. This
one reads the *prose* for the opposite leak: a title, description or quoted
commit message that announces a security fix before the advisory is out.
On an ASF project that is a policy violation, not a style point: "Messages
associated with any commits should not make any reference to the security
nature of the commit" (https://www.apache.org/security/committers.html).
Any project with a private disclosure process has the same problem; the
policy sentence is just ASF's wording of it.

Deterministic: a regex scan, no model call, so a prompt-injected description
cannot talk it out of a match (Magpie's own test for the port below). One
finding per PR at ``important``, listing every match with the clause it sits
in, and it never blocks anything: it is a draft the operator reads like any
other, and the anti-pattern gate learns the false positives.

Ported from Apache Magpie's ``pr-management`` tool at apache/magpie commit
dadb315caa8b881fc1d2195bef03223dac25915a (Apache-2.0):
``tools/pr-management/src/pr_management/triage/signals.py`` (the phrase
list, ``SECURITY_PATTERNS``) and
``tools/pr-management/src/pr_management/code_review/scans.py``
(``_context`` and ``security_disclosure``, including the rule that keeps
"exploitable" over the "exploit" inside it). The phrase list shares its core
with ``security.fix_language.MAGPIE_SECURITY_KEYWORDS`` (Magpie's narrower
``issue-fix`` guard); the extra phrases below are pr-management's. Commit
messages are not scanned: frank's clone tracks the default branch only and
the forge clients have no commits fetcher yet, so the title and body are
the public-facing text this check reads.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.checks import BaseCheck
from franktheunicorn.security.fix_language import CVE_RE, MAGPIE_SECURITY_KEYWORDS

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig
    from franktheunicorn.core.models import PullRequest
    from franktheunicorn.review.backends.base import PRContext

logger = logging.getLogger(__name__)

#: pr-management's additions to the issue-fix list already in
#: ``fix_language``: the "security <noun>" family and a few named classes.
PR_MANAGEMENT_PHRASES: tuple[str, ...] = (
    "security issue",
    "security bug",
    "security flaw",
    "security patch",
    "authorization bypass",
    "insecure deserialization",
    "exploit",
    "rce",
)


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Word-bounded, with ``\\s+`` where the phrase has a space (Magpie's build)."""
    return re.compile(r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b", re.IGNORECASE)


#: ``(label, pattern)``; the CVE id first because it is the unambiguous one.
DISCLOSURE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CVE ID", CVE_RE),
    *(
        (phrase, _phrase_pattern(phrase))
        for phrase in dict.fromkeys(MAGPIE_SECURITY_KEYWORDS + PR_MANAGEMENT_PHRASES)
    ),
)

_ASF_POLICY_URL = "https://www.apache.org/security/committers.html"


@dataclass(frozen=True)
class DisclosureMatch:
    """One hit: where it is, the text that matched, the clause around it."""

    location: str
    matched_text: str
    context: str


def _context(text: str, start: int, end: int) -> str:
    """The clause around a match: the sentence it sits in, joined across line breaks."""
    flat = re.sub(r"\s+", " ", text)
    offset = len(re.sub(r"\s+", " ", text[:start]))
    stop = max(flat.rfind(". ", 0, offset), flat.rfind(", ", 0, offset))
    left = stop + 2 if stop != -1 else 0
    right_candidates = [i for i in (flat.find(". ", offset), flat.find(", ", offset)) if i != -1]
    right = min(right_candidates) if right_candidates else len(flat)
    return flat[left:right].strip().rstrip(".")


def scan_disclosure(places: list[tuple[str, str]]) -> list[DisclosureMatch]:
    """Every security-saying match in ``[(location, text), ...]``, pruned.

    One match per pattern per place (the first), and when two matches at the
    same place nest ("exploit" inside "exploitable") only the longer stays.
    """
    matches: list[DisclosureMatch] = []
    for where, text in places:
        for _label, pattern in DISCLOSURE_PATTERNS:
            for match in pattern.finditer(text or ""):
                if any(
                    m.location == where and m.matched_text.lower() in match.group(0).lower()
                    for m in matches
                ):
                    continue
                matches.append(
                    DisclosureMatch(
                        location=where,
                        matched_text=match.group(0),
                        context=_context(text, match.start(), match.end()),
                    )
                )
                break
    return [
        m
        for m in matches
        if not any(
            o is not m
            and o.location == m.location
            and m.matched_text.lower() in o.matched_text.lower()
            and len(o.matched_text) > len(m.matched_text)
            for o in matches
        )
    ]


class SecurityDisclosureCheck(BaseCheck):
    """Flags public PR text that announces a security fix."""

    name = "security-disclosure"

    def __init__(self, governance: str = "standard") -> None:
        self._governance = governance

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        # Unused: this check uses ``scan`` and never calls a model.
        return "", ""

    def scan(
        self,
        pr: PullRequest,
        diff: str,
        backend_config: LLMBackendConfig | None,
    ) -> list[ReviewFinding]:
        matches = scan_disclosure([("title", pr.title or ""), ("body", pr.body or "")])
        if not matches:
            logger.debug("security-disclosure: PR #%d title/body are clean", pr.number)
            return []
        logger.info(
            "security-disclosure: PR #%d names a security fix in its %s (%s)",
            pr.number,
            ", ".join(sorted({m.location for m in matches})),
            ", ".join(m.matched_text for m in matches),
        )
        return [
            ReviewFinding(
                title="security-disclosure: possible undisclosed security fix",
                body=self._body(matches),
                severity="important",
                confidence=0.8,
            )
        ]

    def _body(self, matches: list[DisclosureMatch]) -> str:
        lines = [
            "The PR title or description contains language that reads as a security "
            "vulnerability fix:",
            "",
        ]
        for m in matches:
            lines.append(f"- {m.location}: `{m.matched_text}` — “{m.context}”")
        lines.append("")
        if self._governance == "asf":
            lines.append(
                "ASF policy requires that no reference to the security nature of a commit "
                "appear in public-facing content until the CVE is formally announced "
                f"({_ASF_POLICY_URL})."
            )
        else:
            lines.append(
                "If this is a security fix, the public text should not say so until the "
                "advisory is out."
            )
        lines.append(
            "Before merging: confirm the disclosure process for this fix is complete. If it "
            "is not, this should go through the private security channel rather than a "
            "public PR, and the title, description and commit messages reworded."
        )
        return "\n".join(lines)
