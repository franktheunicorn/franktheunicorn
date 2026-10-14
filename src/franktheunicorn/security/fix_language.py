"""Deterministic scan for security-saying wording on a fix branch.

The fix agent is told in prose to name its branch innocuously and to scrub
security wording from the commit (``fix_agent._FIX_PROMPT``). Prose is a
request to a model; this is the check. Before the operator opens the public
PR, the branch name (and, where a checkout is at hand, the commit subjects)
is scanned for a CVE id and for the phrases that say "this is a security fix"
to anyone reading the fork's branch list. A hit does not block anything,
because frank never opens the PR itself: it is a red badge on the report page
and a WARNING in the log, so the disclosure risk is seen before the click.

The phrase list is ported from Apache Magpie's ``issue-fix`` skill guard
(``plugins/magpie-security/skills/issue-fix/guards/security_language.py`` at
apache/magpie commit dadb315caa8b881fc1d2195bef03223dac25915a, Apache-2.0),
which keeps it deliberately narrow to limit false positives; frank adds a few
terms of its own and the report's own identifiers as dynamic terms, since the
exact CVE id or the reporter's name in a branch name is the leak that matters
most.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: ``CVE-2026-12345`` anywhere in the text, case-insensitive.
CVE_RE = re.compile(r"\bCVE-\d{4}-\d{3,}\b", re.IGNORECASE)

#: Magpie's curated phrase list (see the module docstring), lower-cased
#: substrings. Narrow on purpose: "security" alone would flag every branch
#: that touches a module with that word in its path.
MAGPIE_SECURITY_KEYWORDS: tuple[str, ...] = (
    "sql injection",
    "xss",
    "csrf",
    "ssrf",
    "remote code execution",
    "arbitrary code execution",
    "path traversal",
    "directory traversal",
    "privilege escalation",
    "auth bypass",
    "authentication bypass",
    "buffer overflow",
    "heap overflow",
    "use-after-free",
    "security vulnerability",
    "security fix",
    "exploitable",
)

#: frank's additions. ``advisory`` and ``security@`` name the process rather
#: than the bug; ``vuln`` catches the ``vuln-fix`` branch an agent reaches for
#: when told not to say "security".
FRANK_SECURITY_KEYWORDS: tuple[str, ...] = (
    "advisory",
    "security@",
    "vuln",
    "exploit",
    "cve fix",
)

SECURITY_KEYWORDS: tuple[str, ...] = MAGPIE_SECURITY_KEYWORDS + FRANK_SECURITY_KEYWORDS

#: Branch names use ``-`` and ``_`` where prose uses spaces; fold them before
#: matching so ``sql-injection-fix`` trips ``sql injection``.
_SEPARATORS_RE = re.compile(r"[-_/.]+")


def _normalise(text: str) -> str:
    return _SEPARATORS_RE.sub(" ", text).lower()


def scan(text: str, extra_terms: Iterable[str] = ()) -> list[str]:
    """Every security-saying token in *text*, sorted and de-duplicated.

    *extra_terms* are the report's own identifiers (its CVE id, the reporter's
    name, the scanner finding id): anything that ties a public branch back to
    a private report. Terms shorter than four characters are ignored, since a
    two-letter name matches everything.
    """
    if not text:
        return []
    hits: set[str] = set()
    for match in CVE_RE.finditer(text):
        hits.add(match.group(0).upper())
    lowered = _normalise(text)
    for keyword in SECURITY_KEYWORDS:
        if keyword in lowered:
            hits.add(keyword)
    for term in extra_terms:
        term = (term or "").strip()
        if len(term) < 4:
            continue
        if _normalise(term) in lowered:
            hits.add(term)
    return sorted(hits)


def scan_branches(names: Iterable[str], extra_terms: Iterable[str] = ()) -> list[str]:
    """:func:`scan` over several branch names, each hit prefixed by its branch."""
    extra = tuple(extra_terms)
    out: list[str] = []
    for name in names:
        for hit in scan(name, extra):
            out.append(f"{name}: {hit}")
    return out
