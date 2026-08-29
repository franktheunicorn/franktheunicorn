"""When did this hole get introduced, and which releases therefore have it?

The version mapper asks whether the cited files are *present* on each branch.
This asks the harder and more useful question: which commit put the vulnerable
code there, and which released tags contain that commit. ``git tag --contains``
is a definitive answer where file presence is a guess — a file existing on
``branch-3.5`` says nothing about whether the missing check was there yet.

Two ways in, best first:

* the report ships a patch, so the lines it *removes* are the vulnerable code
  itself. ``git log -S`` (pickaxe) on the longest of them dates the commit that
  first introduced that exact text. This is the real answer.
* no patch, so all we can date is when the cited file was added. That is a floor,
  not an answer, and the row says so.

The newest introducing commit across the cited paths is the one reported: the
vulnerable state was only fully assembled once the last piece landed.

Git only, no agent, so this is cents rather than dollars. It shares the
verifier's checkout and its ``enabled`` gate, and like the version mapper it
never runs by itself — a button, a checkbox or ``--find-introduction``.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from franktheunicorn.security.verifier import (
    _checkout,
    _default_branch,
    _local_checkout,
    refresh_from_upstream,
    resolve_verifier_reviewer,
)
from franktheunicorn.security.version_map import extract_report_paths

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig
    from franktheunicorn.core.models import SecurityReport
    from franktheunicorn.review.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 120

#: A pickaxe search walks history for every commit touching the path, so it is
#: much more expensive than an ls-tree. Real reports cite a handful of files.
MAX_PATHS = 10

#: Shortest removed line worth pickaxing. Below this it matches half the repo —
#: a lone ``}`` dates to the first commit and tells you nothing.
MIN_NEEDLE_CHARS = 24

#: Longest needle handed to ``git log -S``. Long lines are usually reformatted
#: somewhere in history, and a needle that never matches is a wasted walk.
MAX_NEEDLE_CHARS = 200

#: Tags that name a release rather than a checkpoint. Matched case-insensitively.
_RELEASE_TAG_RE = re.compile(r"^v?\d+\.\d+(\.\d+)?$", re.IGNORECASE)

#: How many release tags to keep. Measured on apache/spark: a 2014 commit is
#: contained by 254 tags, 86 of them real releases — a wall nobody reads, in a
#: column and a template. The earliest few plus the count is the answer; see
#: :attr:`IntroductionRun.release_count` for the total.
MAX_RELEASES = 12


@dataclass
class PathOrigin:
    """Where one cited path came from."""

    path: str
    commit: str = ""
    #: Author timestamp, UTC. None when git gave us nothing usable.
    when: datetime | None = None
    subject: str = ""
    #: ``"patch-line"`` (pickaxed the vulnerable text) or ``"file-added"`` (a
    #: floor only). The difference decides how much the answer is worth.
    method: str = ""
    error: str = ""


@dataclass
class IntroductionRun:
    """What the scan made of one report."""

    origins: list[PathOrigin] = field(default_factory=list)
    #: The newest origin across paths — when the hole was fully assembled.
    commit: str = ""
    when: datetime | None = None
    subject: str = ""
    method: str = ""
    #: Release tags containing :attr:`commit`, oldest first, capped at
    #: :data:`MAX_RELEASES`.
    releases: list[str] = field(default_factory=list)
    #: How many release tags actually contain it, before the cap.
    release_count: int = 0
    #: Non-empty when the scan could not run. Distinct from "ran, found nothing".
    error: str = ""
    stale_warning: str = ""
    duration_seconds: float = 0.0

    def summary(self) -> str:
        if self.error:
            return f"Introduction scan did not run: {self.error}"
        if not self.commit:
            return "Introduction scan ran but could not date any cited path."
        when = self.when.date().isoformat() if self.when else "unknown date"
        lead = (
            "Vulnerable code introduced"
            if self.method == "patch-line"
            else "Cited file first added"
        )
        if not self.releases:
            releases = " No released tag contains it — unreleased so far."
        else:
            # Earliest, not the whole list: a 2014 commit is in 86 Spark releases
            # and "from v1.0.0 onwards" is the sentence an advisory needs.
            releases = (
                f" Present in {self.release_count} release(s), from {self.releases[0]} onwards."
            )
        return f"{lead} in {self.commit[:12]} ({when}): {self.subject}.{releases}"


def patch_needles(patch: str) -> list[str]:
    """Removed lines from *patch*, longest first — the vulnerable text itself.

    A ``-`` line in a fix is the code being taken out, which is exactly what to
    search history for. ``---`` headers are excluded, and so is anything short
    enough to match unrelated code.
    """
    needles: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("-") or line.startswith("---"):
            continue
        text = line[1:].strip()
        if len(text) < MIN_NEEDLE_CHARS:
            continue
        needles.append(text[:MAX_NEEDLE_CHARS])
    # Longest first: the most specific needle is the least likely to match
    # boilerplate somewhere else in history.
    return sorted(dict.fromkeys(needles), key=len, reverse=True)


def find_introduction(report: SecurityReport, operator_config: OperatorConfig) -> IntroductionRun:
    """Date the vulnerable code and list the releases that contain it.

    Never raises. Same honesty contract as the rest of the security pipeline: a
    scan that could not start carries ``error``, which is a different thing from
    a scan that ran and could not date anything.
    """
    from franktheunicorn.review.tool_executor import make_executor

    started = time.monotonic()
    run = IntroductionRun()
    verifier = operator_config.security_triage.verifier
    if not verifier.enabled:
        run.error = "security_triage.verifier.enabled is false"
        logger.info("Introduction scan skipped: %s", run.error)
        return run

    project = report.project
    if project is None:
        run.error = "the report has no project, so there is no history to search"
        logger.info("Introduction scan skipped for report #%s: %s", report.pk, run.error)
        return run

    reviewer = resolve_verifier_reviewer(operator_config, verifier)
    if reviewer is None:
        run.error = f"no agent_cli_reviewers entry named {verifier.reviewer!r}"
        return run

    paths = extract_report_paths(report)[:MAX_PATHS]
    if not paths:
        run.error = (
            "no source paths could be extracted from the report or its existing "
            "verification evidence, so there is no file whose history to search"
        )
        logger.info("Introduction scan skipped for report #%s: %s", report.pk, run.error)
        return run

    executor = make_executor(reviewer.remote)
    cwd = executor.prepare_repo(
        project.owner,
        project.repo,
        local_path=_local_checkout(project.owner, project.repo),
        workspace_subdir=verifier.workspace_subdir,
    )
    if not cwd:
        run.error = (
            "no checkout could be prepared — for remote.mode ssh check ssh_command "
            "and remote_workspace_dir; for local mode check FRANK_REPOS_DIR"
        )
        logger.warning("Introduction scan for report #%s: %s", report.pk, run.error)
        return run

    run.stale_warning = refresh_from_upstream(executor, cwd)
    if run.stale_warning:
        logger.warning(
            "Could not refresh %s from upstream before dating report #%s (%s). Going "
            "ahead against whatever the checkout already had.",
            cwd,
            report.pk,
            run.stale_warning,
        )

    # History search wants the default branch: a release branch's log stops at
    # the fork, which would date a hole to the branch rather than to its origin.
    default = _default_branch(executor, cwd)
    if not default or not _checkout(executor, cwd, default):
        run.error = f"could not check out the default branch ({default or 'unknown'})"
        logger.warning("Introduction scan for report #%s: %s", report.pk, run.error)
        return run

    needles = patch_needles(report.proposed_patch or "")
    logger.info(
        "Dating report #%s across %d path(s) on %s, %s",
        report.pk,
        len(paths),
        default,
        f"pickaxing {len(needles)} removed line(s)" if needles else "by file-add date only",
    )
    for path in paths:
        run.origins.append(_origin_of(executor, cwd, path, needles))

    _summarise(run)
    if run.commit:
        tags = _releases_containing(executor, cwd, run.commit)
        run.release_count = len(tags)
        run.releases = tags[:MAX_RELEASES]
    run.duration_seconds = time.monotonic() - started
    logger.info("Report #%s: %s", report.pk, run.summary())
    return run


def _origin_of(executor: ToolExecutor, cwd: str, path: str, needles: list[str]) -> PathOrigin:
    """Date one path: pickaxe the vulnerable text, else fall back to file-add."""
    for needle in needles:
        found = _pickaxe(executor, cwd, path, needle)
        if found is not None:
            return found
    return _file_added(executor, cwd, path)


_LOG_FORMAT = "--format=%H%x1f%at%x1f%s"


def _pickaxe(executor: ToolExecutor, cwd: str, path: str, needle: str) -> PathOrigin | None:
    """Oldest commit whose diff on *path* added *needle*, or None if it never did.

    ``git log -S`` is newest-first, so the introducing commit is the last line.
    ``--pickaxe-regex`` is deliberately off: the needle is source code and
    treating its brackets as syntax would match nothing.
    """
    result = executor.run(
        ["git", "log", _LOG_FORMAT, "-S", needle, "--", path],
        cwd=cwd,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result is None or not result.ok:
        return None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    origin = _parse_log_line(path, lines[-1], "patch-line")
    return origin if origin.commit else None


def _file_added(executor: ToolExecutor, cwd: str, path: str) -> PathOrigin:
    """When *path* was added. A floor on the introduction date, not an answer."""
    result = executor.run(
        ["git", "log", _LOG_FORMAT, "--diff-filter=A", "--follow", "--", path],
        cwd=cwd,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result is None or not result.ok:
        detail = "no result" if result is None else (result.stderr or "").strip()[:200]
        return PathOrigin(path=path, error=detail or "git log failed")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return PathOrigin(path=path, error="no history for this path on the default branch")
    return _parse_log_line(path, lines[-1], "file-added")


def _parse_log_line(path: str, line: str, method: str) -> PathOrigin:
    parts = line.split("\x1f")
    if len(parts) < 3:
        return PathOrigin(path=path, error=f"could not parse git log output: {line[:120]}")
    commit, stamp, subject = parts[0].strip(), parts[1].strip(), parts[2].strip()
    when: datetime | None = None
    try:
        when = datetime.fromtimestamp(int(stamp), tz=UTC)
    except (ValueError, OverflowError, OSError):
        when = None
    return PathOrigin(path=path, commit=commit, when=when, subject=subject, method=method)


def _summarise(run: IntroductionRun) -> None:
    """Pick the newest dated origin: the hole is only there once all of it is.

    A pickaxed origin beats a file-add one outright — comparing "when this code
    appeared" against "when some other file was created" and taking the later
    would report the floor as the answer.
    """
    dated = [(o.when, o) for o in run.origins if o.commit and o.when is not None]
    if not dated:
        return
    pickaxed = [pair for pair in dated if pair[1].method == "patch-line"]
    when, best = max(pickaxed or dated, key=_origin_sort_key)
    run.commit, run.when, run.subject, run.method = best.commit, when, best.subject, best.method


def _origin_sort_key(pair: tuple[datetime | None, PathOrigin]) -> datetime:
    """Sort key for :func:`_summarise`, whose input is filtered to dated origins."""
    return pair[0] or datetime.min.replace(tzinfo=UTC)


def _releases_containing(executor: ToolExecutor, cwd: str, commit: str) -> list[str]:
    """Release tags containing *commit*, oldest version first.

    This is the payoff: a definitive list of shipped versions carrying the code,
    where file presence could only ever be circumstantial. Checkpoint and
    release-candidate tags are filtered out — a maintainer acts on ``v3.5.0``.
    """
    result = executor.run(
        ["git", "tag", "--contains", commit, "--sort=v:refname"],
        cwd=cwd,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result is None or not result.ok:
        detail = "no result" if result is None else (result.stderr or "").strip()[:200]
        logger.info("Could not list tags containing %s: %s", commit[:12], detail)
        return []
    return [tag.strip() for tag in result.stdout.splitlines() if _RELEASE_TAG_RE.match(tag.strip())]


def persist_introduction(report: SecurityReport, run: IntroductionRun) -> None:
    """Store the run on the report. A failed scan does not wipe a good earlier one."""
    if run.error:
        return
    report.introduced_commit = run.commit[:64]
    report.introduced_at = run.when
    report.introduced_method = run.method
    report.introduced_releases = run.releases
    report.introduced_release_count = run.release_count
    report.introduced_summary = "\n".join([run.summary(), *(_origin_line(o) for o in run.origins)])
    report.save(
        update_fields=[
            "introduced_commit",
            "introduced_at",
            "introduced_method",
            "introduced_releases",
            "introduced_release_count",
            "introduced_summary",
        ]
    )


def _origin_line(origin: PathOrigin) -> str:
    if origin.error:
        return f"  {origin.path}: {origin.error}"
    when = origin.when.date().isoformat() if origin.when else "unknown date"
    return f"  {origin.path}: {origin.commit[:12]} ({when}, {origin.method}) {origin.subject}"
