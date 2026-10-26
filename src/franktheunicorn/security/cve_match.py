"""Match a security report to an ASF CVE — published or allocated.

Two questions, two answers:

* **Published** (``cve_api.list_public_cves`` over the unauthenticated
  ``/publicjson`` list, state=PUBLIC): the ASF has already published a CVE
  for this issue. That is a *duplicate* signal, not a "stage valid" — the
  report is likely a dupe of an already-public CVE, and the operator decides
  (dupe → close, or the published fix was incomplete → keep valid). Mirrors
  ``_check_duplicates``: surface the link, never set ``status``. Title
  similarity only, no model call; the public list is small and the titles are
  short.
* **Allocated but not published** (``cve_api.fetch_record`` over the
  Bearer-token ``/cve5/json/<id>`` endpoint, state in RESERVED/DRAFT/REVIEW/
  READY): the ASF has accepted the vuln and is processing it. That *is* the
  "tentatively set validated" — stronger than any model hedge, because the
  ASF already accepted it. Stage ``valid``. Frank cannot *list* these (the
  list routes need a browser session the Bearer token cannot reach), so it
  looks up candidate ids the report itself names or the operator linked.

``status`` stays the operator's field in both cases; the machine only stages
``auto_triage_status``.
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

#: Above this, a published CVE's title is treated as a dupe signal for the
#: report. High on purpose: a false positive points the operator at the wrong
#: CVE record; 0.6 is "most meaningful tokens shared" measured against the
#: spark public list.
ASF_MATCH_THRESHOLD = 0.6

#: States that mean "the ASF has accepted this vuln but not published it yet".
#: PUBLIC is the dupe case (handled by the public match); REJECT is not.
ALLOCATED_STATES = frozenset({"RESERVED", "DRAFT", "REVIEW", "READY"})

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

#: CVE ids the report text itself names — the candidate set for the allocated
#: lookup. The reporter often cites the CVE they're reporting against.
_CVE_ID_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)


@dataclass(frozen=True)
class ASFCVEMatch:
    """One ASF CVE that matches a report, published or allocated.

    ``kind`` is "public" (a title-similarity match against a published CVE —
    a dupe signal) or "allocated" (an exact-id match against an
    allocated-but-not-published CVE — stages valid). ``score`` is the
    title-similarity score for public matches; 0.0 for allocated (exact id).
    """

    cve_id: str
    kind: str
    title: str = ""
    state: str = ""
    score: float = 0.0


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
) -> ASFCVEMatch | None:
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

    best: ASFCVEMatch | None = None
    for cve in public_cves:
        if not cve.title:
            continue
        score = _similarity(haystack, cve.title)
        if best is None or score > best.score:
            best = ASFCVEMatch(
                cve_id=cve.cve_id,
                kind="public",
                title=cve.title,
                state=cve.state,
                score=score,
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


def extract_candidate_cve_ids(report: SecurityReport) -> list[str]:
    """CVE ids the report itself names, plus the operator's ``matched_cve_id``.

    The candidate set for the allocated-but-not-published lookup: the reporter
    often cites the CVE they're reporting against, and the operator may have
    linked one. Deduped, upper-cased, in CVE-YYYY-NNNNN form.
    """
    seen: set[str] = set()
    ids: list[str] = []
    for source in (report.matched_cve_id, report.title, report.raw_text or ""):
        for match in _CVE_ID_RE.finditer(source):
            cve_id = match.group(0).upper()
            if cve_id not in seen:
                seen.add(cve_id)
                ids.append(cve_id)
    return ids


def public_cve_to_match_entry(match: ASFCVEMatch) -> dict[str, object]:
    """The ``cve_matches`` row for a published-ASF-CVE dupe signal."""
    return {
        "cve_id": match.cve_id,
        "description": match.title,
        "cvss_score": None,
        "status": match.state or "PUBLIC",
        "published": "",
        "source": "asf-public",
        "match_score": round(match.score, 3),
    }


def allocated_cve_to_match_entry(cve_id: str, state: str, title: str) -> dict[str, object]:
    """The ``cve_matches`` row for an allocated-but-not-published ASF CVE."""
    return {
        "cve_id": cve_id,
        "description": title,
        "cvss_score": None,
        "status": state,
        "published": "",
        "source": "asf-allocated",
    }
