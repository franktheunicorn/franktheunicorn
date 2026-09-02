"""Fetch origin, then answer two questions about the backlog with git alone.

Both are questions the operator otherwise answers by hand, one report at a time,
by reading branch names and ``git log``:

* **Which branch carries the fix for this?** A CVE id has been assigned and
  somebody asks what ships it. The answer is usually already sitting in origin —
  a topic branch whose name contains the CVE id, a commit that mentions the
  scanner's finding id, the branch frank's own fix agent pushed. See
  :func:`match_fix_branches`, which writes ``fixed_in_branch`` when the evidence
  is a literal id match and leaves a suggestion when it is softer than that.
* **Has this already been fixed?** :mod:`franktheunicorn.security.recheck` asks
  a cloud agent to read a month of commits, which costs real money and comes
  back a judgement call. When the report ships a proposed patch, git answers it
  outright: ``git apply --check -R`` succeeds only if the patch's own change is
  already in the tree. See :func:`scan_already_fixed`.

Git only, no agent, no model — cents rather than dollars, and definitive where
the agent recheck is a guess. Both are still slow: a fetch plus two ``git log``
calls per branch per project, and a checkout per branch group. They are worker
commands and the buttons say so.

Neither has an ``enabled`` flag of its own. They ride the verifier's, and its
``agent_cli_reviewers`` entry, because that is where "how do I reach a checkout
of this project" is already written down — and a button press is the consent, so
a flag in front of it would be the invisible half of two gates in series. See
:class:`SecurityVerifierConfig` for the last time that was tried.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from django.db.models import QuerySet
from django.utils import timezone

from franktheunicorn.core.models import SecurityReport
from franktheunicorn.security.fix_agent import base_branch_for
from franktheunicorn.security.verifier import (
    _checkout,
    _default_branch,
    _local_checkout,
    origin_refs_by_recency,
    refresh_from_upstream,
    resolve_verifier_reviewer,
)
from franktheunicorn.security.version_map import extract_report_paths, patch_apply_check

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig, SecurityBranchScanConfig
    from franktheunicorn.core.models import Project
    from franktheunicorn.review.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 120

#: Confidence at or above which the scan writes ``fixed_in_branch`` itself
#: rather than only suggesting it.
#:
#: Exactly two signals clear it, and both are somebody deliberately naming *this*
#: vulnerability: a branch whose name contains the CVE id, and a branch our own
#: fix agent pushed for this report. Everything else — including a CVE or finding
#: id merely *mentioned* in a commit message — sits below, because mentioning is
#: not fixing. "Add a regression test for CVE-2025-1234" and "Revert the
#: CVE-2025-1234 fix, it broke the build" both put that id in the default
#: branch's message index, and at 0.85 both auto-wrote ``fixed_in_branch=master``
#: for a hole that is still open — the exact failure an automatic write has to be
#: incapable of.
AUTO_TIE_CONFIDENCE = 0.9

#: How much of one commit message is kept. Enough for a release-notes paragraph
#: naming a CVE; not a squashed changelog per commit times 300 branches.
_MAX_MESSAGE_CHARS = 2000

#: A finding id shorter than this is not evidence. Scanner ids run from ``f1``
#: to ``bug_120``, and a bare ``f1`` matches a commit message token somewhere on
#: every branch in the repo.
MIN_FINDING_ID_CHARS = 4


def usable_finding_id(finding_id: str) -> str:
    """*finding_id* as a token worth matching commit messages against, or "".

    Three ways a scanner's id is not evidence, and all three were reachable:

    * too short — see :data:`MIN_FINDING_ID_CHARS`.
    * **all digits.** ``scan_archive._finding_id`` takes whatever the manifest
      put there, so ``"id": 1234`` becomes ``"1234"``, which passes the length
      floor and then matches ``[SPARK-1234][SQL] Fix NPE in Foo`` — the token set
      of 2000 Spark commits contains almost every four-digit number, so this was
      near-certain to hit the default branch and name a branch off an unrelated
      JIRA number.
    * **more than one token.** ``bug-86`` tokenises to ``bug`` and ``86``, so as
      a single needle it matched nothing ever; as two it would match everything.
      Declined rather than guessed at.
    """
    stripped = finding_id.strip().lower()
    if len(stripped) < MIN_FINDING_ID_CHARS or stripped.isdigit():
        return ""
    tokens = _TOKEN_RE.findall(stripped)
    return tokens[0] if len(tokens) == 1 else ""


#: CVE ids, wherever they appear. Extracted with a regex rather than left to the
#: tokeniser because the id contains hyphens, which is exactly what the tokeniser
#: splits on.
#:
#: ``{4,19}`` to match ``sheet_sync``'s validator, which gates both writers of
#: ``matched_cve_id``. At ``{4,7}`` this read ``cve-2025-12345678`` as
#: ``cve-2025-1234567``, so the branch literally named after a long CVE never
#: matched its own report — and handed a 0.92 auto-tie to whichever report
#: carried the truncated id.
_CVE_RE = re.compile(r"cve-\d{4}-\d{4,19}")

#: What counts as one word for an id match. Branch names split on ``-`` and
#: ``/`` here, which is the point: ``fix-f001-npe`` has to yield ``f001``.
_TOKEN_RE = re.compile(r"[a-z0-9_]+")

#: The three verdicts :func:`scan_already_fixed` writes to
#: ``SecurityReport.recheck_status``. ``unclear`` is this module's addition and
#: it is the honest answer to "the patch neither applies nor reverse-applies":
#: the code moved, and git cannot say whether it moved because somebody fixed
#: this or because somebody refactored around it.
FIXED = "likely-fixed"
STILL_VALID = "still-valid"
UNCLEAR = "unclear"

#: Written to ``SecurityReport.recheck_method`` so a reverse-apply — which is
#: proof — is never read as the cloud agent's opinion of a commit log.
GIT_METHOD = "git"


@dataclass
class BranchEvidence:
    """One branch's evidence, indexed so every report is a set lookup against it.

    The naive shape here was a list of messages and a substring search per
    report per branch. On a real backlog that is 500 reports x 300 branches x a
    100 KB haystack, which is tens of gigabytes of scanning for a button press.
    So the branch is tokenised once and the reports ask about membership;
    quoting the commit that matched is deferred to the handful that actually do.
    """

    name: str
    #: Every token in the branch name alone. Separate from the commit tokens
    #: because a name match is the stronger signal — somebody typed it on
    #: purpose.
    name_tokens: set[str] = field(default_factory=set)
    name_cves: set[str] = field(default_factory=set)
    tokens: set[str] = field(default_factory=set)
    cves: set[str] = field(default_factory=set)
    #: needle -> the **original-cased** subject of the first commit carrying it,
    #: for the reason string. Built in the same pass that fills ``tokens`` and
    #: ``cves``, which replaced keeping every message around and scanning them.
    #:
    #: Both halves of that were wrong. The list was up to 4 MB per branch (2000
    #: default-branch commits x 2000 chars) and ``quote`` linear-scanned it once
    #: per matching candidate — and the "hits are rare so a scan is fine"
    #: defence is exactly backwards for the default branch, whose whole point is
    #: to match a lot. And it scanned the *lowercased* copy, so the reason read
    #: "tighten foo validation" for a commit titled "[SPARK-12345][SQL] Tighten
    #: Foo validation" — which no ``git log --grep`` or GitHub search will find,
    #: leaving the operator unable to check the one claim the reason exists to
    #: make checkable.
    subjects: dict[str, str] = field(default_factory=dict)
    #: Paths the branch's own commits touched. Empty for the default branch —
    #: see :func:`gather_branch_evidence`.
    paths: set[str] = field(default_factory=set)

    def quote(self, needle: str) -> str:
        """The original-cased subject of the first commit carrying *needle*."""
        return self.subjects.get(needle, "")


@dataclass
class BranchMatch:
    """One branch's claim on one report."""

    branch: str
    confidence: float
    reason: str


@dataclass
class BranchMatchRun:
    """What :func:`match_fix_branches` did to one project."""

    project: str = ""
    #: Set when the run could not start at all. Distinct from "ran and matched
    #: nothing", the same as everywhere else in this package.
    error: str = ""
    stale_warning: str = ""
    branches_scanned: int = 0
    reports_considered: int = 0
    #: Reports that came out with a branch attached, at any confidence.
    matched: int = 0
    #: Of those, the ones confident enough that ``fixed_in_branch`` was written.
    applied: int = 0

    def summary(self) -> str:
        if self.error:
            return f"Branch match did not run for {self.project}: {self.error}"
        line = (
            f"{self.project}: scanned {self.branches_scanned} branch(es) against "
            f"{self.reports_considered} report(s) — {self.matched} matched, "
            f"{self.applied} branch(es) recorded."
        )
        if self.stale_warning:
            line += f" Could not fetch origin ({self.stale_warning}), so this is "
            line += "whatever the checkout already had."
        return line


@dataclass
class FixedScanRun:
    """What :func:`scan_already_fixed` did to one project."""

    project: str = ""
    error: str = ""
    stale_warning: str = ""
    reports_considered: int = 0
    fixed: int = 0
    still_valid: int = 0
    unclear: int = 0
    #: Reports in the set that could not be answered, by reason. Counted rather
    #: than dropped: "0 fixed" and "nothing was checkable" look identical from a
    #: flash message otherwise.
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def summary(self) -> str:
        if self.error:
            return f"Fixed-scan did not run for {self.project}: {self.error}"
        line = (
            f"{self.project}: checked {self.reports_considered} report(s) — "
            f"{self.fixed} likely fixed, {self.still_valid} still valid, "
            f"{self.unclear} unclear."
        )
        for reason, count in sorted(self.skipped.items()):
            line += f" Skipped {count}: {reason}."
        if self.stale_warning:
            line += f" Could not fetch origin ({self.stale_warning}), so this is "
            line += "whatever the checkout already had."
        return line


@dataclass
class _Checkout:
    """A prepared, freshly-fetched checkout, or the reason there isn't one."""

    executor: ToolExecutor | None = None
    cwd: str = ""
    default_branch: str = ""
    error: str = ""
    stale_warning: str = ""


def _prepare(project: Project, operator_config: OperatorConfig) -> _Checkout:
    """Borrow the verifier's checkout and fetch origin into it.

    The fetch is the whole point of both callers — "look at origin" means origin
    as it is now, not as it was whenever the review poller last ran — so unlike
    the verifier this carries a failed fetch through to the operator rather than
    only logging it. It still presses on: a stale tree answers most of these
    correctly, and refusing outright gives the operator nothing at all.
    """
    from franktheunicorn.review.tool_executor import make_executor

    verifier = operator_config.security_triage.verifier
    if not verifier.enabled:
        return _Checkout(
            error=(
                "security_triage.verifier.enabled is false in operator.yaml, and both "
                "git sweeps borrow its checkout"
            )
        )
    reviewer = resolve_verifier_reviewer(operator_config, verifier)
    if reviewer is None:
        have = ", ".join(rc.name for rc in operator_config.agent_cli_reviewers) or "none"
        return _Checkout(
            error=(
                f"no agent_cli_reviewers entry named {verifier.reviewer!r} (configured: "
                f"{have}), so there is no checkout config to borrow"
            )
        )

    executor = make_executor(reviewer.remote)
    cwd = executor.prepare_repo(
        project.owner,
        project.repo,
        local_path=_local_checkout(project.owner, project.repo),
        workspace_subdir=verifier.workspace_subdir,
    )
    if not cwd:
        return _Checkout(
            error=(
                "no checkout could be prepared — for remote.mode ssh check ssh_command "
                "and remote_workspace_dir; for local mode check FRANK_REPOS_DIR"
            )
        )
    stale = refresh_from_upstream(executor, cwd)
    if stale:
        logger.warning(
            "Could not fetch origin into %s for %s (%s). Scanning whatever the checkout "
            "already had, which will miss any branch pushed since.",
            cwd,
            project.full_name,
            stale,
        )
    default = _default_branch(executor, cwd)
    if not default:
        return _Checkout(
            error="no default branch could be resolved in the checkout", stale_warning=stale
        )
    return _Checkout(executor=executor, cwd=cwd, default_branch=default, stale_warning=stale)


def list_origin_branches(
    executor: ToolExecutor, cwd: str, config: SecurityBranchScanConfig, default_branch: str
) -> list[str]:
    """Every recently-touched branch on origin, default first, then newest.

    Deliberately *not* ``verifier.select_branches``: that filters to named
    version branches, which is right for "which release lines does this hole
    ship in" and exactly wrong here. A branch carrying a fix is called
    ``fix-cve-2025-1234`` or ``holden/tighten-foo-validation``, and any name
    filter is guaranteed to drop it.

    So the count cap and the activity window are the whole cost control, and the
    window is wider than the verifier's: a fix branch pushed and then forgotten
    in February is the case this exists for. The ref listing itself is shared with
    ``select_branches`` (:func:`origin_refs_by_recency`) — this used to be a copy
    of it, and what the copy dropped is the paragraph below.

    *default_branch* is seeded first and exempt from the cap. Required rather
    than defaulted, because a default of ``""`` is silently the bug this
    parameter exists to fix — which
    ``select_branches`` is careful about and the first version of this dropped
    along with the name filter it was copied from. Without it, a repo with 300
    live dependabot/renovate/CI branches fills every slot ahead of a master
    committed to yesterday: ``gather_branch_evidence`` is then never called with
    the default branch at all, ``max_default_commits`` is dead config, and the
    fix that landed on master three months ago — the case that constant exists
    to reach — is never looked at. The activity window can drop it too, on a
    project dormant longer than a year.
    """
    refs = origin_refs_by_recency(executor, cwd)
    if refs is None:
        # Degrade to the default branch, which ``select_branches`` does and the
        # first version of this did not: ``_prepare`` has already resolved it by
        # now, and a transient for-each-ref failure otherwise threw away the one
        # branch most likely to carry a landed fix and failed the whole project's
        # sweep with "no branches could be listed".
        logger.warning(
            "Scanning the default branch (%s) of %s only.",
            default_branch or "none resolved",
            cwd,
        )
        return [default_branch] if default_branch else []
    cutoff = time.time() - config.branch_active_within_days * 86400
    names: list[str] = [default_branch] if default_branch else []
    seen = set(names)
    for name, committed in refs:
        if name in seen:
            continue
        if committed < cutoff:
            continue
        names.append(name)
        seen.add(name)
        if len(names) >= config.max_branches + (1 if default_branch else 0):
            logger.info(
                "Stopping at the %d most recently committed branches in %s; anything "
                "older is not scanned (security_triage.branch_scan.max_branches).",
                config.max_branches,
                cwd,
            )
            break
    return names


def gather_branch_evidence(
    executor: ToolExecutor,
    cwd: str,
    branch: str,
    default_branch: str,
    config: SecurityBranchScanConfig,
) -> BranchEvidence:
    """Commit messages, and for a topic branch the paths it touched.

    Two ``git log`` calls rather than one with ``--name-only`` and a custom
    format: parsing those two interleaved is the kind of clever that breaks on a
    commit touching no files.

    **Merge commits are included, and that is not incidental.** This started with
    ``--no-merges`` and it made both message signals dead on any repo using the
    ordinary GitHub flow. Reproduced against a throwaway repository: merge
    ``holden/foo`` into master with "Merge pull request #42 from holden/foo /
    Fix CVE-2026-1234 in Foo", and ``git log --no-merges master`` does not
    contain that CVE anywhere — it is named in the merge commit, not the topic
    commit — while ``git log master`` does. So the flag hid exactly the sentence
    a maintainer writes when they land a security fix, and the sweep reported
    "scanned 300 branches, 0 matched", which is indistinguishable from "no fix
    exists".

    A merged topic branch then has an empty ``default..branch`` range and
    contributes nothing, which is correct rather than a gap: once the fix is on
    the default branch, the default branch is the answer to what carries it, and
    the topic branch still matches on its own name.

    The default branch contributes messages only. Its range is not a topic
    branch's handful of commits — on the projects this is aimed at it is
    thousands — so its path set would be most of the repo, which matches every
    report and is therefore evidence about none of them. Its message range is
    deeper for the same reason: a fix that landed on master three months ago is
    precisely the thing worth finding.
    """
    evidence = BranchEvidence(name=branch)
    evidence.name_tokens = set(_TOKEN_RE.findall(branch.lower()))
    evidence.name_cves = set(_CVE_RE.findall(branch.lower()))
    evidence.tokens |= evidence.name_tokens
    evidence.cves |= evidence.name_cves

    if branch == default_branch:
        rev = f"origin/{branch}"
        limit = config.max_default_commits
        want_paths = False
    else:
        rev = f"origin/{default_branch}..origin/{branch}"
        limit = config.max_commits_per_branch
        want_paths = True

    messages = executor.run(
        ["git", "log", f"-n{limit}", "--format=%s%n%b%n\x1e", rev],
        cwd=cwd,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if messages is None or not messages.ok:
        # A branch deleted between the listing and here, or a clone too shallow
        # to reach the merge base. One branch's evidence, not the run — so DEBUG
        # for a topic branch.
        #
        # Not for the default branch. That is the deepest and most valuable
        # message range there is, and losing it silently (a shallow clone, a 120s
        # timeout on a Spark-sized log) leaves the sweep reporting "scanned 300
        # branch(es), 0 matched" with nothing above DEBUG saying why —
        # indistinguishable from "no fix exists anywhere", which is the failure
        # the --no-merges note above is about. A configured tool that can't run
        # logs at WARNING.
        detail = "no result" if messages is None else (messages.stderr or "").strip()[:200]
        if branch == default_branch:
            logger.warning(
                "git log failed for the DEFAULT branch %s in %s (%s) — the branch sweep's "
                "main message signal is missing for this project, so a fix that landed on "
                "%s will not be found.",
                branch,
                cwd,
                detail,
                branch,
            )
        else:
            logger.debug("git log failed for %s in %s: %s", branch, cwd, detail)
        return evidence
    for chunk in messages.stdout.split("\x1e"):
        original = chunk.strip()[:_MAX_MESSAGE_CHARS]
        if not original:
            continue
        subject = original.splitlines()[0][:100]
        lowered = original.lower()
        # First commit carrying a needle wins, and the log is newest-first, so
        # the quoted commit is the most recent one that mentions it.
        for needle in (*_TOKEN_RE.findall(lowered), *_CVE_RE.findall(lowered)):
            evidence.subjects.setdefault(needle, subject)
        evidence.tokens.update(_TOKEN_RE.findall(lowered))
        evidence.cves.update(_CVE_RE.findall(lowered))

    if want_paths:
        touched = executor.run(
            ["git", "log", f"-n{limit}", "--name-only", "--format=", rev],
            cwd=cwd,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if touched is None or not touched.ok:
            detail = "no result" if touched is None else (touched.stderr or "").strip()[:200]
            logger.debug("git log --name-only failed for %s in %s: %s", branch, cwd, detail)
            return evidence
        for line in touched.stdout.splitlines():
            path = line.strip()
            if path:
                evidence.paths.add(path)
            if len(evidence.paths) >= config.max_paths_per_branch:
                # Excluding the default branch was never the real guard. A
                # long-lived release branch is ~1500 cherry-picks off master and
                # touches most of a subtree, so its path set overlaps nearly
                # every report's cited files and every row renders "maybe
                # branch-3.5 (0.45)" — the useless-suggestion outcome
                # score_branch's docstring says makes the column unread. What
                # matters is whether the set is *broad*, so that is what is
                # tested: a set that hit the cap is discarded rather than
                # truncated, because a truncated one is arbitrary as well as
                # broad.
                logger.info(
                    "Branch %s touches at least %d paths; dropping its path signal "
                    "(a set that wide matches every report).",
                    branch,
                    config.max_paths_per_branch,
                )
                evidence.paths = set()
                break
    return evidence


@dataclass
class _Candidate:
    """A report reduced to the handful of things a branch gets compared against."""

    pk: int
    cve: str
    finding: str
    #: The branch frank's own fix agent pushed for this report, if it did.
    fix_branch: str
    paths: set[str]
    #: A branch this sweep already applied. Non-empty means "tried"; combined
    #: with an empty ``fixed_in_branch`` on the row it means the operator took it
    #: back off, which is the supported rejection and must not be undone.
    rejected_branch: str = ""
    best: BranchMatch | None = None

    def offer(self, match: BranchMatch | None) -> None:
        """Keep *match* if it beats what we already had.

        Ties go to the incumbent, and the branch list arrives
        most-recently-committed first, so a tie means the newer branch wins.
        """
        if match is None:
            return
        if self.best is None or match.confidence > self.best.confidence:
            self.best = match


def score_branch(candidate: _Candidate, evidence: BranchEvidence) -> BranchMatch | None:
    """How strongly *evidence* claims to be the branch that fixes *candidate*.

    Five signals. Only the first two are allowed to become an answer by
    themselves — see :data:`AUTO_TIE_CONFIDENCE` for why the line is drawn
    there and not one tier lower.

    1. It is the branch frank's own fix agent pushed for this report. Not a
       heuristic at all — we know, because we asked for it. Only fires once that
       branch is on *origin*: the fix agent pushes to the operator's fork, so
       this is the signal for a fix that has since been pushed upstream.
    2. The CVE id is in the branch name. Somebody typed it there on purpose.
    3. The CVE appears in a commit message. Strong, and still not an answer:
       reverts and regression tests name a CVE without fixing it.
    4. The scanner's finding id is in the branch name, or in a commit message.
       Weaker than a CVE because the id is the scanner's private label —
       see :func:`usable_finding_id` for the three shapes that aren't evidence.
    5. The report's cited paths overlap the paths the branch touched. A real
       hint and nowhere near an answer — a refactor of the same file scores
       here.

    What is deliberately *not* a signal: fuzzy title-versus-subject wording.
    Every version of it we sketched matched "fix NPE in Foo" against half the
    repo's commits, and a suggestion column full of wrong branches is one the
    operator stops reading — which costs more than the matches it would add.
    """
    if candidate.fix_branch and candidate.fix_branch == evidence.name:
        return BranchMatch(
            branch=evidence.name,
            confidence=0.98,
            reason="frank's own fix agent pushed this branch for this report",
        )
    if candidate.cve:
        needle = candidate.cve.lower()
        if needle in evidence.name_cves:
            return BranchMatch(
                branch=evidence.name,
                confidence=0.95,
                reason=f"branch name contains {candidate.cve}",
            )
        if needle in evidence.cves:
            subject = evidence.quote(needle)
            reason = f"a commit on this branch mentions {candidate.cve}"
            return BranchMatch(
                branch=evidence.name,
                confidence=0.75,
                reason=f"{reason}: {subject}" if subject else reason,
            )
    if candidate.finding:
        needle = candidate.finding
        if needle in evidence.name_tokens:
            return BranchMatch(
                branch=evidence.name,
                confidence=0.7,
                reason=f"branch name contains the finding id {candidate.finding}",
            )
        if needle in evidence.tokens:
            subject = evidence.quote(needle)
            reason = f"a commit on this branch mentions the finding id {candidate.finding}"
            return BranchMatch(
                branch=evidence.name,
                confidence=0.6,
                reason=f"{reason}: {subject}" if subject else reason,
            )
    overlap = candidate.paths & evidence.paths
    if overlap:
        shown = ", ".join(sorted(overlap)[:3])
        return BranchMatch(
            branch=evidence.name,
            # 0.35 for one file, 0.45 for three or more. Capped well under
            # AUTO_TIE_CONFIDENCE however many files line up: a branch that
            # rewrote the whole package touches all of them and fixes none.
            confidence=0.30 + 0.05 * min(len(overlap), 3),
            reason=f"this branch touches the cited file(s): {shown}",
        )
    return None


def _candidates_for(project: Project) -> list[_Candidate]:
    """Reports of *project* with no branch recorded yet, most important first.

    Reports whose status says no fix is owed are left out: for those, no branch
    *is* the answer rather than a gap, which is the same call
    ``cve_without_branch_q`` makes.
    """
    reports = (
        SecurityReport.objects.filter(project=project, fixed_in_branch="")
        .exclude(status__in=SecurityReport.NO_FIX_OWED_STATUSES)
        .prefetch_related("verifications")
        .order_by("-priority", "pk")
    )
    candidates = []
    for report in reports:
        candidates.append(
            _Candidate(
                pk=report.pk,
                cve=report.matched_cve_id.strip(),
                finding=usable_finding_id(report.finding_id),
                fix_branch=report.fix_branch.strip(),
                paths=set(extract_report_paths(report)),
                # A branch this sweep applied and the operator then cleared is a
                # rejection, and re-applying it every run is how a suggestion the
                # operator took back comes straight back. See _apply.
                rejected_branch=(report.branch_match_branch if report.branch_match_applied else ""),
            )
        )
    return candidates


def match_fix_branches(project: Project, operator_config: OperatorConfig) -> BranchMatchRun:
    """Fetch origin and tie a branch to every report of *project* that lacks one.

    Never raises: a run that could not start carries ``error``, which is a
    different thing from having looked and found nothing.

    A confident match (see :data:`AUTO_TIE_CONFIDENCE`) is written straight into
    ``fixed_in_branch`` — that is what "tie the branches in automatically"
    means, and an id appearing verbatim in a branch name is not the sort of
    guess worth a confirmation click. Everything softer lands in the
    ``branch_match_*`` columns for the operator to accept or ignore.

    What it never does is overwrite. The write re-tests ``fixed_in_branch=""``
    rather than trusting the snapshot it selected from, because a sweep over a
    few hundred reports takes real wall-clock time and an answer the operator
    typed at minute three is not in a list built at minute zero.
    """
    run = BranchMatchRun(project=project.full_name)
    config = operator_config.security_triage.branch_scan

    # Before the fetch, not after. `projects_with_open_reports` only promises an
    # open report exists, not that any of them needs a branch — so a project
    # whose backlog is fully tied paid for a full `git fetch --all` of a
    # Spark-sized mirror and then logged "every report already has a branch".
    # Two buttons times N projects times one pointless fetch each.
    candidates = _candidates_for(project)
    run.reports_considered = len(candidates)
    if not candidates:
        logger.info("Branch match for %s: every report already has a branch.", project.full_name)
        return run

    checkout = _prepare(project, operator_config)
    run.stale_warning = checkout.stale_warning
    if checkout.error or checkout.executor is None:
        run.error = checkout.error
        logger.info("Branch match skipped for %s: %s", project.full_name, run.error)
        return run

    branches = list_origin_branches(
        checkout.executor, checkout.cwd, config, checkout.default_branch
    )
    run.branches_scanned = len(branches)
    if not branches:
        run.error = "no branches could be listed in the checkout"
        logger.warning("Branch match for %s: %s", project.full_name, run.error)
        return run

    logger.info(
        "Matching %d branch(es) of %s against %d report(s) with no branch recorded.",
        len(branches),
        project.full_name,
        len(candidates),
    )
    for branch in branches:
        evidence = gather_branch_evidence(
            checkout.executor, checkout.cwd, branch, checkout.default_branch, config
        )
        for candidate in candidates:
            candidate.offer(score_branch(candidate, evidence))

    now = timezone.now()
    for candidate in candidates:
        if candidate.best is not None:
            run.matched += 1
            if _apply(candidate, now):
                run.applied += 1
    logger.info("%s", run.summary())
    return run


def _apply(candidate: _Candidate, now: datetime) -> bool:
    """Write one candidate's match. True when it became ``fixed_in_branch``.

    Two things this must not do, both of which it did.

    It must not write onto a report the operator ruled on while the sweep was
    running. A sweep over a few hundred reports takes real wall-clock time, so
    the ``.update()`` re-tests the row rather than trusting the list selected at
    minute zero — and it re-tests *status* as well as the branch, because a
    verdict of ``invalid`` typed at minute three leaves ``fixed_in_branch``
    empty, and stamping a fix branch onto a report explicitly ruled
    not-a-vulnerability is worse than losing the suggestion.

    And it must not re-apply a branch the operator took back off. Clearing the
    field is the documented way to reject one; without ``rejected_branch`` the
    same name scored the same 0.95 on the next run and came straight back, which
    is the loop ``fix_agent``'s ``fix_superseded`` exists to avoid.

    Which is why declining a rejected branch leaves ``branch_match_applied``
    alone. That flag, set with an empty ``fixed_in_branch``, *is* the rejection
    record — so writing the freshly-computed False over it destroyed the thing
    the decline was reading. Measured over three sweeps: applied, cleared by the
    operator, declined-and-forgotten, re-applied. It also stops the templates
    re-offering the rejected name as a suggestion, since both gate on
    ``not branch_match_applied``.

    One rejection is remembered, not a list: the two signals that can auto-tie
    are narrow enough that two branches competing for one report is not a case
    worth a JSON column for.
    """
    match = candidate.best
    if match is None:
        return False
    rejected = bool(candidate.rejected_branch) and match.branch == candidate.rejected_branch
    applied = match.confidence >= AUTO_TIE_CONFIDENCE and not rejected
    fields: dict[str, object] = {
        "branch_match_branch": match.branch[:200],
        "branch_match_confidence": match.confidence,
        "branch_match_reason": match.reason[:500],
        "branch_matched_at": now,
        "updated_at": now,
    }
    if not rejected:
        fields["branch_match_applied"] = applied
    if applied:
        fields["fixed_in_branch"] = match.branch
    written = (
        SecurityReport.objects.filter(pk=candidate.pk, fixed_in_branch="")
        .exclude(status__in=SecurityReport.NO_FIX_OWED_STATUSES)
        .update(**fields)
    )
    if written and applied:
        logger.info("Report #%d tied to branch %s (%s)", candidate.pk, match.branch, match.reason)
    return bool(written and applied)


def _fixed_scan_candidates(project: Project) -> QuerySet[SecurityReport]:
    """Reports of *project* worth asking git whether they are already fixed.

    Wider than the agent recheck's set, which is untriaged reports only. This
    one is git, so it costs two ``git apply --check`` calls per report and can
    afford to include the ones the operator ruled ``valid`` — a report you have
    accepted and not yet fixed is exactly the one where "it landed last Tuesday"
    is worth knowing. A report with a branch recorded is left out: that question
    is already answered.
    """
    return (
        SecurityReport.objects.filter(project=project, status__in=("new", "valid"))
        .filter(fixed_in_branch="")
        .exclude(proposed_patch="")
        .order_by("-priority", "pk")
    )


def scan_already_fixed(project: Project, operator_config: OperatorConfig) -> FixedScanRun:
    """Ask git whether each report's proposed patch is already in the tree.

    ``git apply --check -R`` succeeds only when the patch's change is present,
    which makes this proof rather than a guess — and it is why the verdict lands
    in the same ``recheck_status`` column the cloud agent writes, with
    ``recheck_method`` recording which of the two answered.

    Checked against the branch the report is *about*, because a hole reported
    against ``branch-3.5`` is not fixed by a commit on master. That branch comes
    from ``fix_base_branch`` when a fix run has recorded one, and otherwise from
    the archive's own name via ``fix_agent.base_branch_for`` — which is the whole
    imported backlog, since ``fix_base_branch`` has exactly one writer and it is
    reachable only from a Fix-button press. Reading only the column meant every
    report from ``spark-branch-3.5-findings.zip`` had its patch reverse-applied
    against master, where it neither applies nor reverse-applies, for a wall of
    "unclear — the code moved" over a base the archive label had all along.
    Reports are grouped by branch so each is checked out once.

    Never raises. A report with no proposed patch is not in the set at all —
    there is nothing for git to reverse-apply — and the button says so before
    queueing rather than reporting a silent zero afterwards. ``skipped`` is for
    reports that were in the set and still couldn't be answered.
    """
    run = FixedScanRun(project=project.full_name)
    config = operator_config.security_triage.branch_scan

    # Grouping needs three columns, not the whole row. Loading full instances
    # meant every candidate's `proposed_patch` (up to 400 KB), `raw_text` and
    # `parsed_poc` were resident before the first git call — hundreds of MB on a
    # 500-report backlog, for patches each needed for a couple of seconds. The
    # patch itself is fetched per group, below.
    shortlist = list(
        _fixed_scan_candidates(project).only("pk", "fix_base_branch", "source_archive")[
            : config.max_reports_per_scan
        ]
    )
    if not shortlist:
        logger.info(
            "Fixed-scan for %s: no open report carries a proposed patch, so git has "
            "nothing to check. The cloud-agent recheck is the one that can read commits.",
            project.full_name,
        )
        return run
    total = _fixed_scan_candidates(project).count()
    if total > len(shortlist):
        # Said out loud. A silent top-N reads as "we checked everything".
        logger.info(
            "Fixed-scan for %s: checking the %d highest-priority of %d patch-carrying "
            "report(s) (security_triage.branch_scan.max_reports_per_scan); press again "
            "after triaging those to reach the rest.",
            project.full_name,
            len(shortlist),
            total,
        )

    checkout = _prepare(project, operator_config)
    run.stale_warning = checkout.stale_warning
    if checkout.error or checkout.executor is None:
        run.error = checkout.error
        logger.info("Fixed-scan skipped for %s: %s", project.full_name, run.error)
        return run

    grouped: dict[str, list[int]] = {}
    for stub in shortlist:
        branch = stub.fix_base_branch.strip() or base_branch_for(stub) or checkout.default_branch
        grouped.setdefault(branch, []).append(stub.pk)

    logger.info(
        "Reverse-applying %d proposed patch(es) for %s across %d branch(es).",
        len(shortlist),
        project.full_name,
        len(grouped),
    )
    now = timezone.now()
    for branch, pks in grouped.items():
        if not _checkout(checkout.executor, checkout.cwd, branch):
            for _ in pks:
                run.skip(f"origin/{branch} could not be checked out")
            continue
        # One group's patches at a time, so the resident set is a group rather
        # than the backlog.
        for report in SecurityReport.objects.filter(pk__in=pks).only("pk", "proposed_patch"):
            if _scan_one(checkout.executor, checkout.cwd, report, branch, run, now):
                run.reports_considered += 1
    logger.info("%s", run.summary())
    return run


def _scan_one(
    executor: ToolExecutor,
    cwd: str,
    report: SecurityReport,
    branch: str,
    run: FixedScanRun,
    now: datetime,
) -> bool:
    """One report's verdict on an already-checked-out *branch*. False if skipped."""
    patch = report.proposed_patch
    reversed_ok = patch_apply_check(executor, cwd, patch, reverse=True)
    if reversed_ok is None:
        # Deliberately not "too big, or binary": git also exits 128 for anything
        # it can't parse as a diff, and `executor.run` returns None for a
        # timeout or a dropped SSH connection. Naming only the archive's
        # possible faults misdiagnosed an infrastructure failure as a defective
        # bundle, across every remaining report in the group at once.
        run.skip(
            "git could not read it as a patch, or the git call itself failed "
            "(too big, binary, not a diff, or the executor died)"
        )
        return False
    if reversed_ok:
        verdict = FIXED
        reason = (
            f"the proposed patch reverse-applies cleanly on origin/{branch}, so the change "
            "it makes is already in the tree"
        )
    else:
        # Tested for None separately, not left to fall through an `elif`. Both
        # calls can come back "could not even try" — a 120s timeout, a dropped
        # SSH connection — and None is falsy, so one blip mid-sweep otherwise
        # read as "does not apply" and stamped the confident, authoritative
        # "the code moved" on every remaining report in the group.
        forward_ok = patch_apply_check(executor, cwd, patch, reverse=False)
        if forward_ok is None:
            run.skip("the forward git apply check could not be run (timeout, or the executor died)")
            return False
        if forward_ok:
            verdict = STILL_VALID
            reason = (
                f"the proposed patch still applies cleanly on origin/{branch}, so the code it "
                "changes is untouched"
            )
        else:
            verdict = UNCLEAR
            reason = (
                f"the proposed patch neither applies nor reverse-applies on origin/{branch} — the "
                "code moved, and git can't say whether that was this fix or a refactor around it"
            )
    # Re-tested rather than trusted: the same wall-clock race the branch matcher
    # guards against, and here a verdict written over the operator's own ruling
    # would be worse than a verdict lost.
    rows = SecurityReport.objects.filter(
        pk=report.pk, status__in=("new", "valid"), fixed_in_branch=""
    )
    if verdict == UNCLEAR:
        # A non-answer must not overwrite an answer. The cloud recheck may have
        # already paid an agent to read the commit log and written "likely-fixed"
        # with a citation; this sweep's patch failing to apply on a branch it may
        # not even be the right base for is no reason to replace that with "the
        # code moved" and lose the reasoning. Empty means nobody has answered, so
        # "unclear" is strictly better than nothing there.
        rows = rows.filter(recheck_status="")
    written = rows.update(
        recheck_status=verdict,
        recheck_reason=reason,
        recheck_method=GIT_METHOD,
        rechecked_at=now,
        updated_at=now,
    )
    if not written:
        run.skip(
            "an existing recheck verdict was left in place"
            if verdict == UNCLEAR
            else "the operator ruled on it while the scan was running"
        )
        return False
    if verdict == FIXED:
        run.fixed += 1
        logger.info("Report #%d looks already fixed on %s", report.pk, branch)
    elif verdict == STILL_VALID:
        run.still_valid += 1
    else:
        run.unclear += 1
    return True


def projects_with_open_reports() -> list[Project]:
    """Projects that have a report either sweep could act on.

    Project-less reports are excluded throughout: both sweeps need a repo to
    look at, and a report with no project has none.
    """
    from franktheunicorn.core.models import Project

    project_ids = (
        SecurityReport.objects.filter(project__isnull=False)
        .exclude(status__in=SecurityReport.NO_FIX_OWED_STATUSES)
        .values_list("project_id", flat=True)
        .distinct()
    )
    return list(Project.objects.filter(pk__in=list(project_ids)).order_by("pk"))
