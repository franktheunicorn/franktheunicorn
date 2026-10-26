"""Match a security report to a published ASF CVE.

``cve_api.list_public_cves`` fetches the ASF CVE process's public list — every
CVE the ASF has published for a PMC, with its title. This module answers the
dedup question the NVD lookup in ``security.triage`` does not: has the ASF
*already* opened a CVE for this issue? A published CVE that matches the report
is strong evidence the report is a real, known hole, so a match stages the
report as ``valid`` (tentatively — ``status`` stays the operator's field; the
machine only stages ``auto_triage_status``).

Title-similarity only, no model call: the public list is small (a handful per
PMC) and the titles are short, so a token-overlap score over the report's title
and parsed component is enough. The threshold is high on purpose — a false
match stages a real report as a known CVE and points the operator at the wrong
record, and the operator's Agree click is the one that applies it.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from franktheunicorn.security.cve_api import list_public_cves, resolve_pmc

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import SecurityReport

logger = logging.getLogger(__name__)

#: Above this, a report is tentatively staged ``valid`` on an ASF title match.
#: High on purpose: a false positive points the operator at the wrong CVE
#: record; 0.6 is "most meaningful tokens shared" measured against the spark
#: public list.
ASF_MATCH_THRESHOLD = 0.6

#: Present in every Spark title, so they dilute Jaccard without discriminating.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "in",
        "of",
        "on",
        "via",
        "and",
        "or",
        "to",
        "for",
        "is",
        "by",
        "with",
        "apache",
        "spark",
        "vulnerability",
        "vuln",
    }
)


@dataclass(frozen=True)
class ASFPublicCVEMatch:
    """The best published ASF CVE that matches a report, or None below threshold."""

    cve_id: str
    title: str
    score: float
    state: str = ""


def _tokenize(text: str) -> frozenset[str]:
    """Lowercased alnum tokens, stopwords and 1-char tokens dropped."""
    tokens: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", text.lower()):
        if len(raw) <= 1 or raw in _STOPWORDS:
            continue
        tokens.add(raw)
    return frozenset(tokens)


def _similarity(haystack: str, cve_title: str) -> float:
    """How close *haystack* (report title + component) is to a CVE title.

    Jaccard over the meaningful tokens, averaged with a difflib ratio on the
    raw strings so a reordered title ("Spark UI command injection" vs "Apache
    Spark shell command injection vulnerability via Spark UI") still scores
    well when the token overlap does.
    """
    a = _tokenize(haystack)
    b = _tokenize(cve_title)
    if not a or not b:
        return 0.0
    jaccard = len(a & b) / len(a | b)
    ratio = difflib.SequenceMatcher(None, haystack.lower(), cve_title.lower()).ratio()
    return (jaccard + ratio) / 2


def match_report_to_asf_cve(
    report: SecurityReport,
    operator_config: OperatorConfig,
    *,
    project_config: ProjectConfig | None = None,
) -> ASFPublicCVEMatch | None:
    """The best published ASF CVE for *report*'s project, or None below threshold.

    ``project_config`` is taken from the caller when triage already has it and
    reloaded otherwise, so a run over many reports parses the YAML once.
    """
    if report.project is None:
        return None
    if project_config is None:
        from franktheunicorn.config.loader import get_project_config

        project_config = get_project_config(report.project.full_name)
    pmc = resolve_pmc(report.project, project_config)
    if not pmc:
        return None

    haystack = f"{report.title} {report.parsed_component}".strip()
    if not haystack:
        return None

    base_url = operator_config.security_triage.cve_api.base_url
    public_cves = list_public_cves(pmc, base_url=base_url)
    if not public_cves:
        return None

    best: ASFPublicCVEMatch | None = None
    for cve in public_cves:
        if not cve.title:
            continue
        score = _similarity(haystack, cve.title)
        if best is None or score > best.score:
            best = ASFPublicCVEMatch(
                cve_id=cve.cve_id, title=cve.title, score=score, state=cve.state
            )

    if best is None or best.score < ASF_MATCH_THRESHOLD:
        logger.info(
            "ASF public-CVE match for report #%d: none above %.2f (best=%s @ %.2f)",
            report.pk,
            ASF_MATCH_THRESHOLD,
            best.cve_id if best else "—",
            best.score if best else 0.0,
        )
        return None
    logger.info(
        "ASF public-CVE match for report #%d: %s @ %.2f (%s)",
        report.pk,
        best.cve_id,
        best.score,
        best.title[:80],
    )
    return best


def public_cve_to_match_entry(match: ASFPublicCVEMatch) -> dict[str, object]:
    """The ``cve_matches`` row shape for an ASF public-CVE match.

    Mirrors the NVD entries' shape plus a ``source`` marker so the CVE-matches
    table can tell the two apart.
    """
    return {
        "cve_id": match.cve_id,
        "description": match.title,
        "cvss_score": None,
        "status": match.state or "PUBLIC",
        "published": "",
        "source": "asf-public",
        "match_score": round(match.score, 3),
    }
