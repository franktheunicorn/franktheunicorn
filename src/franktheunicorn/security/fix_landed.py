"""Has the fix landed upstream? Answered with git, and only with proof.

The backlog already knows how to find a fix branch (``branch_scan``) and how
to tell whether a proposed patch is already in the tree (``scan_already_fixed``).
This module answers the follow-up for a report that *has* a branch: the branch
frank's fix agent pushed, or the one the operator typed into the verdict form —
did it actually land? "Landed" means the fix commit is an ancestor of the
default branch or a release line (``git merge-base --is-ancestor``), or is in
a release tag (``git tag --contains``), which is the strongest answer there is.

Three shapes of answer, and the distinction is the whole module:

* **proof** — ancestry and tag checks against a freshly-fetched origin. What
  the fix commit *is* is known exactly for frank's own branches (the sha is on
  the row), and for an operator-typed topic branch the tip is the thing tested.
* **a pointer** — the fork branch's upstream PR, found through the GitHub API:
  a merged PR gives the merge commit sha, which survives Spark's squash-merge
  where the branch tip does not.
* **indeterminate** — everything git cannot prove, written only when nothing
  better is on the row. The degenerate case is ``fixed_in_branch`` naming a
  *mainline* branch ("master", "branch-3.5"): there is no topic ref to test,
  and ancestry of a branch against itself is a tautology. Those are the cloud
  agent fallback's job (``recheck.launch_fix_landed_recheck``), not git's.

Verdicts land in ``SecurityReport.fix_landed_*`` — the machine's evidence,
kept separate from ``fix_merged_upstream``, which is the sheet's column. The
split is the ``branch_match_*`` one: evidence and ruling disagree in both
directions often enough that both must stay visible.

Like the sweeps it borrows the verifier's checkout (see ``branch_scan``), and
inherits its gates: no verifier config, no checkout, and the button says so.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
from django.db.models import Q
from django.utils import timezone

from franktheunicorn.core.models import SecurityReport
from franktheunicorn.security.branch_scan import _prepare
from franktheunicorn.security.verifier import select_branches

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig
    from franktheunicorn.core.models import Project
    from franktheunicorn.review.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 120
_GITHUB_API = "https://api.github.com"
_GITHUB_TIMEOUT = 30

#: Reports one sweep will check, highest priority first. Each is a handful of
#: git calls plus at most one GitHub lookup, so this is the wall-clock knob —
#: the branch_scan.max_reports_per_scan argument, for a cheaper operation.
_MAX_REPORTS_PER_SWEEP = 250

#: How many release tags land in the detail JSON. A 2014 fix is in 86 Spark
#: releases; the count is the answer, the list is a sample.
_MAX_TAGS_KEPT = 10

#: An operator-typed branch name simple enough to resolve as ``origin/<name>``
#: without guessing. ``fixed_in_branch`` is free text and often carries
#: commentary; anything else is not a ref.
_SIMPLE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# Verdict vocabulary, matching SecurityReport.FIX_LANDED_STATUS_CHOICES.
MERGED = "merged"
RELEASED = "released"
NOT_MERGED = "not-merged"
NO_FIX_BRANCH = "no-fix-branch"
INDETERMINATE = "indeterminate"

GIT_METHOD = "git"


@dataclass
class FixLandedRun:
    """What :func:`check_project_fixes` did to one project."""

    project: str = ""
    #: Set when the run could not start at all. Distinct from "ran and every
    #: report came back indeterminate", the same as everywhere in this package.
    error: str = ""
    stale_warning: str = ""
    reports_considered: int = 0
    merged: int = 0
    released: int = 0
    not_merged: int = 0
    indeterminate: int = 0
    no_fix_branch: int = 0
    #: Reports in the set that could not even be attempted, by reason.
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def summary(self) -> str:
        if self.error:
            return f"Fix-landed check did not run for {self.project}: {self.error}"
        line = (
            f"{self.project}: checked {self.reports_considered} report(s) — "
            f"{self.released} released, {self.merged} merged, {self.not_merged} not merged, "
            f"{self.indeterminate} indeterminate."
        )
        for reason, count in sorted(self.skipped.items()):
            line += f" Skipped {count}: {reason}."
        if self.stale_warning:
            line += f" Could not fetch origin ({self.stale_warning}), so this is "
            line += "whatever the checkout already had."
        return line


def check_project_fixes(project: Project, operator_config: OperatorConfig) -> FixLandedRun:
    """Check every fix-branch-carrying report of *project* against origin.

    Never raises: a run that could not start carries ``error``, which is a
    different thing from having looked and found nothing provable. Reports
    already known ``released`` are left out — tags do not un-happen, so that
    verdict never needs re-checking.
    """
    run = FixLandedRun(project=project.full_name)
    reports = list(
        SecurityReport.objects.filter(project=project)
        .exclude(status__in=SecurityReport.NO_FIX_OWED_STATUSES)
        .filter(Q(fix_branch__gt="") | Q(fix_branch_sha__gt="") | Q(fixed_in_branch__gt=""))
        .exclude(fix_landed_status=RELEASED)
        .order_by("-priority", "pk")[:_MAX_REPORTS_PER_SWEEP]
    )
    run.reports_considered = len(reports)
    if not reports:
        logger.info(
            "Fix-landed check for %s: no open report carries a fix branch, so there is "
            "nothing to check. The branch sweep is the one that finds branches.",
            project.full_name,
        )
        return run

    checkout = _prepare(project, operator_config)
    run.stale_warning = checkout.stale_warning
    if checkout.error or checkout.executor is None:
        run.error = checkout.error
        logger.info("Fix-landed check skipped for %s: %s", project.full_name, run.error)
        return run

    verifier = operator_config.security_triage.verifier
    branches = select_branches(checkout.executor, checkout.cwd, verifier)
    release_branches = [b for b in branches if b != checkout.default_branch]

    logger.info(
        "Checking whether fixes landed for %d report(s) of %s (default=%s, %d release branch(es)).",
        len(reports),
        project.full_name,
        checkout.default_branch,
        len(release_branches),
    )
    now = timezone.now()
    for report in reports:
        verdict = _check_one(
            checkout.executor,
            checkout.cwd,
            checkout.default_branch,
            release_branches,
            report,
            operator_config,
            now,
        )
        if verdict == RELEASED:
            run.released += 1
        elif verdict == MERGED:
            run.merged += 1
        elif verdict == NOT_MERGED:
            run.not_merged += 1
        elif verdict == NO_FIX_BRANCH:
            run.no_fix_branch += 1
        elif verdict == INDETERMINATE:
            run.indeterminate += 1
        else:
            run.skip(verdict.removeprefix("skipped:"))
    logger.info("%s", run.summary())
    return run


def check_fix_landed(report: SecurityReport, operator_config: OperatorConfig) -> str:
    """One report's answer, for the per-report worker command. Never raises."""
    if report.project is None:
        return "report has no project, so there is no repo to check against"
    checkout = _prepare(report.project, operator_config)
    if checkout.error or checkout.executor is None:
        return f"no checkout: {checkout.error}"
    verifier = operator_config.security_triage.verifier
    branches = select_branches(checkout.executor, checkout.cwd, verifier)
    release_branches = [b for b in branches if b != checkout.default_branch]
    verdict = _check_one(
        checkout.executor,
        checkout.cwd,
        checkout.default_branch,
        release_branches,
        report,
        operator_config,
        timezone.now(),
    )
    report.refresh_from_db()
    return f"{verdict}: {report.fix_landed_detail.get('note', '')}"


def _check_one(
    executor: ToolExecutor,
    cwd: str,
    default_branch: str,
    release_branches: list[str],
    report: SecurityReport,
    operator_config: OperatorConfig,
    now: datetime,
) -> str:
    """Resolve the report's fix ref and test it. Returns the verdict written."""
    mainline = {default_branch, *release_branches}

    # 1. frank's own branch tip sha — the exact commit, known because we asked.
    if report.fix_branch_sha and _sha_exists(executor, cwd, report.fix_branch_sha):
        verdict, detail = _test_ref(
            executor,
            cwd,
            report.fix_branch_sha,
            f"the fix agent's branch tip {report.fix_branch_sha[:12]}",
            default_branch,
            release_branches,
        )
        _write(report, verdict, detail, now)
        return verdict

    # 2. frank's branch, sha unknown or not in the local repo: on origin it is
    #    directly testable; otherwise ask GitHub whether a PR from it merged.
    if report.fix_branch:
        sha = _resolve_ref(executor, cwd, f"origin/{report.fix_branch}")
        if sha:
            verdict, detail = _test_ref(
                executor,
                cwd,
                sha,
                f"origin/{report.fix_branch} ({sha[:12]})",
                default_branch,
                release_branches,
            )
            _write(report, verdict, detail, now)
            return verdict
        verdict, detail = _pr_verdict(
            report, operator_config, executor, cwd, default_branch, release_branches
        )
        _write(report, verdict, detail, now)
        return verdict

    # 3. The operator's branch. Mainline names prove nothing — ancestry of a
    #    branch against itself is a tautology — and free-text lists are tested
    #    ref by ref, strongest answer wins.
    text = report.fixed_in_branch.strip()
    if not text:
        _write(report, NO_FIX_BRANCH, {"note": "no fix branch recorded anywhere"}, now)
        return NO_FIX_BRANCH
    best: tuple[str, dict[str, Any]] | None = None
    tested: list[str] = []
    for token in re.split(r"[,\s]+", text):
        if not _SIMPLE_REF_RE.match(token):
            continue
        if token in mainline:
            continue  # the degenerate case: a mainline branch is not a testable ref
        sha = _resolve_ref(executor, cwd, f"origin/{token}")
        if not sha:
            continue
        tested.append(token)
        verdict, detail = _test_ref(
            executor, cwd, sha, f"origin/{token} ({sha[:12]})", default_branch, release_branches
        )
        if best is None or _rank(verdict) > _rank(best[0]):
            best = (verdict, detail)
    if best is not None:
        _write(report, best[0], best[1], now)
        return best[0]
    _write(
        report,
        INDETERMINATE,
        {
            "note": (
                f"nothing testable in fixed_in_branch {text!r}: mainline branch names and "
                "free text prove nothing to git, and no topic branch named there is on origin. "
                "The agent fallback can read the log instead."
            )
        },
        now,
    )
    return INDETERMINATE


def _rank(verdict: str) -> int:
    """Strongest answer wins when several refs were tested."""
    return {"": 0, INDETERMINATE: 1, NO_FIX_BRANCH: 1, NOT_MERGED: 2, MERGED: 3, RELEASED: 4}.get(
        verdict, 0
    )


def _test_ref(
    executor: ToolExecutor,
    cwd: str,
    sha: str,
    label: str,
    default_branch: str,
    release_branches: list[str],
) -> tuple[str, dict[str, Any]]:
    """Ancestry-check one ref, naming it in the note — unless the answer is
    indeterminate, whose own note (which git call failed) is the useful one."""
    verdict, detail = _ancestry_verdict(executor, cwd, sha, default_branch, release_branches)
    if verdict != INDETERMINATE:
        detail["note"] = f"tested {label}"
    return verdict, detail


def _ancestry_verdict(
    executor: ToolExecutor,
    cwd: str,
    sha: str,
    default_branch: str,
    release_branches: list[str],
) -> tuple[str, dict[str, Any]]:
    """Tag and ancestry checks for a commit known to exist locally."""
    detail: dict[str, Any] = {"checked_ref": sha, "branches": [], "tags": []}

    tags = _tags_containing(executor, cwd, sha)
    if tags is None:
        return INDETERMINATE, {
            "note": f"git tag --contains {sha[:12]} failed (timeout or executor)"
        }
    if tags:
        detail["tags"] = tags[:_MAX_TAGS_KEPT]
        detail["tag_count"] = len(tags)
        return RELEASED, detail

    branches: list[str] = []
    for branch in [default_branch, *release_branches]:
        answer = _is_ancestor(executor, cwd, sha, f"origin/{branch}")
        if answer is None:
            return INDETERMINATE, {
                "note": f"git merge-base against origin/{branch} failed (timeout or executor)"
            }
        if answer:
            branches.append(branch)
    detail["branches"] = branches
    if branches:
        return MERGED, detail
    return NOT_MERGED, detail


def _pr_verdict(
    report: SecurityReport,
    operator_config: OperatorConfig,
    executor: ToolExecutor,
    cwd: str,
    default_branch: str,
    release_branches: list[str],
) -> tuple[str, dict[str, Any]]:
    """The fork branch's upstream PR: merged gives a merge sha to test."""
    from franktheunicorn.security.fix_agent import fork_full_name

    assert report.project is not None  # callers checked before preparing a checkout
    config = operator_config.security_triage.fix_agent
    fork = fork_full_name(report, config, operator_config)
    if not fork:
        return INDETERMINATE, {
            "note": "the fix branch is not on origin and no fork is configured to look for a PR"
        }
    fork_owner = fork.split("/", 1)[0]
    pr = _find_pr(report.project.full_name, fork_owner, report.fix_branch, operator_config)
    if pr is None:
        return INDETERMINATE, {
            "note": "the fix branch is not on origin and the GitHub PR lookup failed (logged)"
        }
    if not pr:
        return INDETERMINATE, {
            "note": (
                f"the fix branch {report.fix_branch} is not on origin and no upstream PR "
                "from it was found"
            )
        }
    detail: dict[str, Any] = {"pr_url": pr.get("html_url", "")}
    merged_at = pr.get("merged_at") or ""
    if not merged_at:
        detail["note"] = "an upstream PR from the fix branch exists and is not merged"
        return NOT_MERGED, detail
    detail["pr_merged_at"] = merged_at
    merge_sha = pr.get("merge_commit_sha") or ""
    # The merge commit is on the target branch by definition, so a squash merge
    # — where the branch tip never enters history — is still answered exactly.
    if merge_sha and _sha_exists(executor, cwd, merge_sha):
        verdict, ancestry = _test_ref(
            executor,
            cwd,
            merge_sha,
            f"the PR merge commit {merge_sha[:12]}",
            default_branch,
            release_branches,
        )
        ancestry.update(detail)
        return verdict, ancestry
    return MERGED, {
        **detail,
        "note": "the upstream PR is merged (merge commit not in the checkout)",
    }


def _find_pr(
    upstream: str, fork_owner: str, branch: str, operator_config: OperatorConfig
) -> dict[str, Any] | None:
    """The PR from ``fork_owner:branch`` against upstream, or {} for none, None on failure.

    One call, button-press volume — no rate-limiter bucket, same as the CVE
    process client.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if operator_config.github_token:
        headers["Authorization"] = f"Bearer {operator_config.github_token}"
    try:
        response = httpx.get(
            f"{_GITHUB_API}/repos/{upstream}/pulls",
            params={"head": f"{fork_owner}:{branch}", "state": "all", "per_page": "5"},
            headers=headers,
            timeout=_GITHUB_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        logger.warning("GitHub PR lookup for %s from %s failed: %s", branch, upstream, exc)
        return None
    if response.status_code != 200:
        logger.warning(
            "GitHub PR lookup for %s from %s returned HTTP %d",
            branch,
            upstream,
            response.status_code,
        )
        return None
    try:
        pulls = response.json()
    except ValueError:
        logger.warning("GitHub PR lookup for %s from %s returned non-JSON", branch, upstream)
        return None
    if not isinstance(pulls, list):
        return None
    for pr in pulls:
        if isinstance(pr, dict) and pr.get("merged_at"):
            return pr
    return pulls[0] if pulls and isinstance(pulls[0], dict) else {}


def _sha_exists(executor: ToolExecutor, cwd: str, sha: str) -> bool:
    result = executor.run(["git", "cat-file", "-t", sha], cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS)
    return bool(result is not None and result.ok)


def _resolve_ref(executor: ToolExecutor, cwd: str, ref: str) -> str:
    """The sha *ref* names, or "" — never raises, never guesses."""
    result = executor.run(
        ["git", "rev-parse", "--verify", "--quiet", ref], cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS
    )
    if result is None or not result.ok:
        return ""
    return result.stdout.strip()


def _is_ancestor(executor: ToolExecutor, cwd: str, sha: str, ref: str) -> bool | None:
    """git merge-base --is-ancestor: exit 0 yes, 1 no, anything else "no answer"."""
    result = executor.run(
        ["git", "merge-base", "--is-ancestor", sha, ref], cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS
    )
    if result is None:
        return None
    if result.ok:
        return True
    # Exit 1 is "not an ancestor"; 128 is "couldn't parse a rev", which is not
    # an answer — the scan_already_fixed rule about git exit codes.
    return False if result.returncode == 1 else None


def _tags_containing(executor: ToolExecutor, cwd: str, sha: str) -> list[str] | None:
    result = executor.run(["git", "tag", "--contains", sha], cwd=cwd, timeout=_GIT_TIMEOUT_SECONDS)
    if result is None or not result.ok:
        return None
    return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


def _write(report: SecurityReport, verdict: str, detail: dict[str, Any], now: datetime) -> None:
    """Write one report's verdict, with the two never-overwrite rules.

    ``indeterminate`` is a non-answer and only fills an empty slot — it must
    not replace a verdict somebody (or some agent) paid for, the
    ``scan_already_fixed`` rule about "unclear". And ``released`` is terminal:
    tags do not un-happen, so nothing overwrites it.
    """
    rows = SecurityReport.objects.filter(pk=report.pk).exclude(
        status__in=SecurityReport.NO_FIX_OWED_STATUSES
    )
    if verdict == INDETERMINATE:
        rows = rows.filter(fix_landed_status="")
    rows = rows.exclude(fix_landed_status=RELEASED)
    written = rows.update(
        fix_landed_status=verdict,
        fix_landed_detail=detail,
        fix_landed_method=GIT_METHOD,
        fix_landed_checked_at=now,
        updated_at=now,
    )
    if not written:
        logger.debug(
            "Fix-landed verdict for report #%d not written (verdict=%s): the row moved "
            "while the check ran.",
            report.pk,
            verdict,
        )
