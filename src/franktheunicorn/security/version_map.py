"""Cheap version mapping: which release lines does this finding likely hit?

The deep verifier answers "is this hole in the code" by putting an agent on
each branch. That is the right answer and it is also why one checkbox on a
143-report archive was refused — thousands of hours of agent time.

This is the other question, and git can answer it. A scanner finding names
files. If those files are still on ``branch-3.5`` the finding likely applies
to 3.5.x; if they were rewritten away, it likely does not. When the archive
shipped a proposed patch, ``git apply --check`` says which trees it still
fits — no agent, just git. No report cap and no branch *count* cap, though
the verifier's ``branch_active_within_days`` and name patterns still decide
which branches exist to look at. File presence and a clean apply are not a
code read — the row says so, and the confidence stays low.

Takes reports that already exist (imported, pasted, whatever). Prefers paths
from an existing verification's evidence when one is there, then the report
text and any proposed patch. Writes :class:`SecurityVerification` rows tagged
``version-map``. A deep-verifier row for the same branch is left alone except
to fill ``version_impact`` when that field is empty — we do not overwrite a
real look with a path check.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from django.utils import timezone

from franktheunicorn.core.models import SecurityVerification
from franktheunicorn.security.verifier import (
    BranchResult,
    VerificationRun,
    _checkout,
    _local_checkout,
    refresh_from_upstream,
    resolve_verifier_reviewer,
    select_branches,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig
    from franktheunicorn.core.models import SecurityReport
    from franktheunicorn.review.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

#: Stored on every row this module writes so the page can tell a path-check
#: from a code read, and so a re-run replaces only our own rows.
VERSION_MAP_AGENT = "version-map"

_GIT_TIMEOUT_SECONDS = 120

#: Extensions we recognise in a citation.
#:
#: Two traps, and both produced a confident "not affected" for a file that was
#: sitting right there on the branch. Alternation is first-match, not
#: longest-match, so a bare ``c`` ahead of ``cpp`` captured ``foo.cpp`` as
#: ``foo.c`` — fixed by sorting longest-first below. And with no boundary after
#: the alternation, ``py`` captured ``App.pyi`` as ``App.py`` and ``h`` captured
#: ``index.html`` as ``index.h`` however the list was ordered — fixed by the
#: ``(?!\w)`` in the pattern. The list is broad because an unlisted extension is
#: a citation that never gets checked.
_SOURCE_EXTENSIONS = (
    "java",
    "scala",
    "kt",
    "py",
    "pyi",
    "js",
    "jsx",
    "ts",
    "tsx",
    "go",
    "rb",
    "c",
    "cc",
    "cpp",
    "cs",
    "h",
    "hpp",
    "rs",
    "swift",
    "php",
    "pl",
    "sh",
    "sql",
    "proto",
    "xml",
    "yaml",
    "yml",
    "json",
    "toml",
    "ini",
    "conf",
    "properties",
    "gradle",
    "html",
    "htm",
    "rst",
    "tf",
)

#: Paths as they appear in reports and evidence. Case-preserving: git is.
#: ``,`` and ``;`` are separators too — dense scanner output writes
#: ``a/Foo.py,b/Bar.py`` and only the first was being picked up.
_PATH_RE = re.compile(
    r"(?:^|[\s`'\"=(,;])((?:[\w.-]+/)+[\w.-]+\.(?:"
    + "|".join(sorted(_SOURCE_EXTENSIONS, key=len, reverse=True))
    + r"))(?!\w)"
)
_PATCH_PATH_RE = re.compile(r"^(?:\+\+\+|---) [ab]/(.+)$", re.MULTILINE)

#: A scanner finding names a handful of files. A novel of a report that
#: mentions two hundred is not something we want 200 x N-branches of git for.
_MAX_PATHS = 30

_DEFAULT_BRANCH_NAMES = frozenset({"master", "main", "develop", "trunk", "HEAD"})


def extract_report_paths(report: SecurityReport) -> list[str]:
    """Cited source paths, first-seen casing, evidence preferred.

    Existing verification evidence is the best source: that is the file the
    agent actually read. The report text and a proposed patch are the fallback
    for a finding that has not been deep-verified yet.
    """
    chunks: list[str] = []
    if report.pk:
        for row in report.verifications.all():
            if row.agent == VERSION_MAP_AGENT:
                continue
            if row.evidence:
                chunks.append(row.evidence)
            if row.summary:
                chunks.append(row.summary)
    chunks.extend(
        [
            report.proposed_patch or "",
            report.parsed_component or "",
            report.title or "",
            (report.raw_text or "")[:12_000],
            report.parsed_poc or "",
        ]
    )
    return _unique_paths("\n".join(chunks))


def _unique_paths(text: str) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for match in (*_PATCH_PATH_RE.finditer(text), *_PATH_RE.finditer(text)):
        # removeprefix, not lstrip: lstrip takes a character *set*, so it ate the
        # leading dot of ".github/workflows/ci.yml" and the path never matched.
        raw = match.group(1).strip().removeprefix("./")
        for candidate in _prefix_candidates(raw):
            if not candidate or candidate.lower() in seen:
                continue
            if candidate.startswith(("a/", "b/")) and candidate[2:].lower() in seen:
                # _PATCH_PATH_RE runs first and strips the prefix authoritatively,
                # so once the bare form is in, the prefixed spelling of a patch
                # header is the same file listed twice.
                continue
            seen.add(candidate.lower())
            paths.append(candidate)
        if len(paths) >= _MAX_PATHS:
            break
    return paths[:_MAX_PATHS]


def _prefix_candidates(raw: str) -> tuple[str, ...]:
    """*raw*, and the same path without git's ``a/``/``b/`` diff prefix.

    Which of the two is the real path is not decidable from the text: ``a/Foo.py``
    is what a diff calls ``Foo.py``, and it is also what a repo with a top-level
    ``a/`` calls a genuine file. The old guard stripped only at two or more
    slashes, so ``a/Foo.py`` and ``b/pom.xml`` kept a prefix that matches nothing
    in ``git ls-tree`` output — and a path that matches nothing is reported as
    "not affected" with a confidence attached.

    Both go in the list instead, and ls-tree picks whichever exists. It costs
    nothing: the branch listing is fetched once and these are set lookups.
    """
    if raw.startswith(("a/", "b/")):
        return (raw, raw[2:])
    return (raw,)


def release_line_from_branch(branch: str) -> str:
    """``branch-3.5`` → ``3.5.x``; default branches → ``unreleased``."""
    if branch in _DEFAULT_BRANCH_NAMES:
        return "unreleased"
    match = re.search(r"(\d+)\.(\d+)", branch)
    if match:
        return f"{match.group(1)}.{match.group(2)}.x"
    return branch


def map_report_versions(report: SecurityReport, operator_config: OperatorConfig) -> VerificationRun:
    """Walk every active release line and record whether the cited files are there.

    Never raises. Same honesty contract as :func:`verify_report`: a run that
    could not start carries ``error``, and that is distinct from "looked and
    found nothing".
    """
    from franktheunicorn.review.tool_executor import make_executor

    run = VerificationRun()
    verifier = operator_config.security_triage.verifier
    if not verifier.enabled:
        run.error = "security_triage.verifier.enabled is false"
        logger.info("Version mapping skipped: %s", run.error)
        return run

    project = report.project
    if project is None:
        run.error = "the report has no project, so there is no repo to check"
        logger.info("Version mapping skipped for report #%s: %s", report.pk, run.error)
        return run

    reviewer = resolve_verifier_reviewer(operator_config, verifier)
    if reviewer is None:
        run.error = f"no agent_cli_reviewers entry named {verifier.reviewer!r}"
        return run

    paths = extract_report_paths(report)
    patch = report.proposed_patch or ""
    if not paths and not patch.strip():
        run.error = (
            "no source paths could be extracted from the report or its existing "
            "verification evidence, so there is nothing to look up on each branch"
        )
        logger.info("Version mapping skipped for report #%s: %s", report.pk, run.error)
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
        logger.warning("Version mapping for report #%s: %s", report.pk, run.error)
        return run

    run.stale_warning = refresh_from_upstream(executor, cwd)
    if run.stale_warning:
        logger.warning(
            "Could not refresh %s from upstream before mapping versions for "
            "report #%s (%s). Going ahead against whatever the checkout already had.",
            cwd,
            report.pk,
            run.stale_warning,
        )

    branches = select_branches(executor, cwd, verifier, unlimited=True)
    run.branches_considered = list(branches)
    if not branches:
        run.error = "no branches could be resolved in the checkout"
        logger.warning("Version mapping for report #%s: %s", report.pk, run.error)
        return run

    logger.info(
        "Mapping versions for report #%s across %d branch(es) using %d path(s)%s",
        report.pk,
        len(branches),
        len(paths),
        " plus a proposed patch" if patch.strip() else "",
    )
    for branch in branches:
        run.results.append(_map_one_branch(executor, cwd, branch, paths, patch))
    _persist_version_map(report, run)
    return run


def _patch_applies(executor: ToolExecutor, cwd: str, branch: str, patch: str) -> bool | None:
    """Whether ``git apply --check`` accepts *patch* on *branch*.

    None if we could not even try (checkout failed or the executor died).
    ``--check`` does not write the tree. Ticket we did not do: a remote
    whose command channel is already stdin (``command_mode: stdin``) drops
    this payload — those need the patch written to a file on the box.
    """
    commit = _checkout(executor, cwd, branch)
    if not commit:
        return None
    result = executor.run(
        ["git", "apply", "--check", "--whitespace=nowarn", "-"],
        cwd=cwd,
        timeout=_GIT_TIMEOUT_SECONDS,
        stdin=patch,
    )
    if result is None:
        return None
    return result.ok


def _map_one_branch(
    executor: ToolExecutor,
    cwd: str,
    branch: str,
    paths: list[str],
    patch: str,
) -> BranchResult:
    started = time.monotonic()
    found: list[str] = []
    if paths:
        listing = executor.run(
            ["git", "ls-tree", "-r", "--name-only", f"origin/{branch}"],
            cwd=cwd,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if listing is None or not listing.ok:
            detail = "no result" if listing is None else (listing.stderr or "").strip()[:200]
            return BranchResult(
                branch=branch,
                verdict="error",
                agent=VERSION_MAP_AGENT,
                summary=(
                    f"Could not list files on origin/{branch}: {detail or 'git ls-tree failed'}."
                ),
                duration_seconds=time.monotonic() - started,
            )
        present = set(listing.stdout.splitlines())
        found = [path for path in paths if path in present]

    applies = _patch_applies(executor, cwd, branch, patch) if patch.strip() else None
    line = release_line_from_branch(branch)
    parts: list[str] = []
    reasons: list[str] = []
    if paths:
        if found:
            parts.append(f"Cited file(s) exist: {', '.join(found[:8])}.")
            reasons.append(f"cited file(s) still present: {', '.join(found[:8])}")
        else:
            parts.append("None of the cited files exist on this branch.")
            reasons.append("none of the cited files exist on this branch")
    if applies is True:
        parts.append("Proposed patch applies cleanly.")
        reasons.append("proposed patch applies cleanly")
    elif applies is False:
        parts.append("Proposed patch does not apply.")
        reasons.append("proposed patch does not apply")
    elif patch.strip():
        parts.append("Could not try the proposed patch (checkout failed).")

    if found or applies is True:
        verdict = "affected"
        confidence = 0.7 if applies is True else 0.45
    elif applies is None and patch.strip():
        # The patch check is half the evidence and it did not run. A file-presence
        # miss alone does not license "not affected" — the patch might well have
        # applied. Same honesty as a failed ls-tree.
        verdict = "error"
        confidence = None
    else:
        verdict = "not-affected"
        confidence = 0.55
    parts.append("File-presence / patch-check, not a code read.")
    return BranchResult(
        branch=branch,
        verdict=verdict,
        confidence=confidence,
        summary=f"{branch} ({line}): {' '.join(parts)}",
        evidence=found or list(paths[:5]),
        version_impact=(
            [{"name": line, "status": verdict, "reason": "; ".join(reasons)}]
            if verdict != "error"
            else []
        ),
        agent=VERSION_MAP_AGENT,
        duration_seconds=time.monotonic() - started,
    )


def _persist_version_map(report: SecurityReport, run: VerificationRun) -> None:
    """Write version-map rows; do not overwrite a deep-verifier verdict.

    A re-run replaces our own rows. A branch the agent already answered keeps
    that verdict; we only fill ``version_impact`` when the agent left it empty.
    A deep-verifier ``error`` row is not an answer to protect — the agent never
    got a look — so it is treated like no row at all and replaced outright.
    """
    now = timezone.now()
    for order, result in enumerate(run.results):
        existing = SecurityVerification.objects.filter(report=report, branch=result.branch).first()
        if (
            existing is not None
            and existing.agent != VERSION_MAP_AGENT
            and existing.verdict != "error"
        ):
            # Never write a path-check status that contradicts a real look.
            # A deep "not-affected" with empty version_impact plus files still
            # on the branch would otherwise make the release-line headline
            # say affected while the branch table says not-affected.
            if (
                not existing.version_impact
                and result.version_impact
                and existing.verdict == result.verdict
            ):
                existing.version_impact = result.version_impact
                existing.save(update_fields=["version_impact"])
            continue
        SecurityVerification.objects.update_or_create(
            report=report,
            branch=result.branch,
            defaults={
                "commit": result.commit,
                "verdict": result.verdict,
                "confidence": result.confidence,
                "summary": result.summary,
                "evidence": "\n".join(result.evidence),
                "version_impact": result.version_impact,
                "agent": VERSION_MAP_AGENT,
                "branch_order": order,
                "raw_output": result.raw_output,
                "duration_seconds": result.duration_seconds,
                "created_at": now,
            },
        )
