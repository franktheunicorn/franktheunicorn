"""Build CVE 5 JSON advisory records from a SecurityReport.

The output is a draft: the dashboard shows it in an editable preview and the
operator pushes it (security.cve_api.update_record) only after reading it.
Every field sourced from the report is attacker-supplied text — that is the
reason for the preview step, and the reason this module fabricates nothing:
no CVSS vectors, no problem types, no version ranges beyond carrying the
operator's own ``affected_versions`` sentence for them to edit.

Update semantics: when the service already holds a record (``existing``),
its ``cveMetadata`` and any non-cna containers are preserved verbatim and
only the cna fields this module manages are replaced. A fresh record mirrors
what the service's own allocate route creates (``cveMetadata.state``
"PUBLISHED" with ``CNA_private.state`` "RESERVED" — odd, but consistent with
the house the record lives in).
"""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from franktheunicorn.core.models import SecurityReport

#: CVE descriptions are prose; past a few thousand chars it is a report, not
#: a description. The operator can restore more in the preview.
_MAX_DESCRIPTION_CHARS = 4000

#: A branch name simple enough to turn into a GitHub tree URL without
#: guessing. ``fixed_in_branch`` is free text and often a list with
#: commentary ("master, branch-4.0 (backport pending)") — anything that is
#: not one bare ref is left out of references rather than URL-ified wrong.
_SIMPLE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def build_cve5_json(
    report: SecurityReport,
    *,
    pmc: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One CVE 5 record for the report's ``matched_cve_id``.

    Raises :class:`ValueError` when the report has no CVE id — the record is
    keyed on it and the service treats a mismatch as a rename.
    """
    cve_id = report.matched_cve_id.strip().upper()
    if not cve_id:
        raise ValueError("report has no matched_cve_id to build a record for")

    if existing is not None:
        record = copy.deepcopy(existing)
        containers = record.setdefault("containers", {})
        if not isinstance(containers, dict):
            containers = record["containers"] = {}
        cna = containers.setdefault("cna", {})
        if not isinstance(cna, dict):
            cna = containers["cna"] = {}
    else:
        record = {
            "dataType": "CVE_RECORD",
            "dataVersion": "5.0",
            "cveMetadata": {"cveId": cve_id, "serial": 1, "state": "PUBLISHED"},
            "CNA_private": {"owner": pmc, "state": "RESERVED"},
        }
        cna = {"title": ""}
        record["containers"] = {"cna": cna}

    cna["title"] = report.title[:500] if report.title else cve_id
    cna["descriptions"] = [{"lang": "en", "value": _description(report)}]
    cna["affected"] = [_affected(report, pmc)]
    references = _references(report)
    if references:
        cna["references"] = references
    return record


def _description(report: SecurityReport) -> str:
    """Impact first, then the triage summary, then the title — the most
    advisory-shaped text the report has, truncated."""
    for text in (report.parsed_impact, report.triage_summary, report.title):
        if text and text.strip():
            return text.strip()[:_MAX_DESCRIPTION_CHARS]
    return report.matched_cve_id


def _affected(report: SecurityReport, pmc: str) -> dict[str, Any]:
    """One affected entry: Apache / the project / the operator's version sentence.

    The sentence is carried as a single version value on purpose: parsing
    "3.5.0 to 3.5.2, 4.0.0 before 4.0.1" into CVE version ranges is a guess,
    and the preview exists so the operator turns this into real ranges.
    """
    product = report.project.repo if report.project else pmc
    version = report.affected_versions.strip() if report.affected_versions else "unknown"
    return {
        "vendor": "Apache",
        "product": product,
        "versions": [{"version": version, "status": "affected"}],
    }


def _references(report: SecurityReport) -> list[dict[str, str]]:
    """Links the record can defend: the fix branch and the report thread."""
    refs: list[dict[str, str]] = []
    if report.project:
        owner = report.project.owner
        repo = report.project.repo
        # The agent's fork branch is a single known ref; the operator's
        # fixed_in_branch only when it is one bare branch name.
        if report.fix_branch and _SIMPLE_REF_RE.match(report.fix_branch):
            refs.append({"url": f"https://github.com/{owner}/{repo}/tree/{report.fix_branch}"})
        if report.fixed_in_branch and _SIMPLE_REF_RE.match(report.fixed_in_branch.strip()):
            refs.append(
                {"url": f"https://github.com/{owner}/{repo}/tree/{report.fixed_in_branch.strip()}"}
            )
    if report.email_message_id:
        refs.append({"url": f"https://lists.apache.org/thread/{report.email_message_id}"})
    return refs
