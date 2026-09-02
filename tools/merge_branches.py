#!/usr/bin/env python3
"""Batch backport helper.

Reads branch names from branches_to_merge.txt (one per line, "#" comments and
blank lines ignored; an optional second column pins the base ref the branch was
cut from, otherwise it is auto-detected).  For each branch it:

  0. Skips anything named in HELD_BRANCHES.txt outright -- a list you keep, one
     branch per line, that nothing here writes to (--hold-list, --ignore-hold-list).
  1. Finds the branch on the fork (default: holdenk/spark), retrying without the
     -aok marker, and asks GitHub for the CI state of that exact commit.  The
     commits themselves are taken from the local branch under the listed name
     (the squashed -aok rewrite) when there is one, or the fork's copy otherwise.
       * still running / queued -> re-checked until it settles, then merged.
                                   When a whole round goes by with nothing
                                   going green, the branch at the head of the
                                   queue is built locally instead of waiting on
                                   it, and merged if that build is clean; the
                                   next round asks GitHub again first, so this
                                   works down the queue one branch at a time
                                   (--no-local-build-stand-in to just poll,
                                   and --no-compile disables it outright --
                                   there is nothing to stand in with).
       * failing                -> held back, then looked at once more after
                                   everything else has been merged; green by
                                   then and it is picked up, still red and it
                                   is appended to FAILING_CI.txt (skipped).
                                   One extra look is the whole budget.  Before
                                   that look, update-bases.sh and then
                                   squash-magic.sh are run over just those
                                   branches, so the -aok/-squashed rewrite that
                                   gets cherry-picked is cut from the fork's
                                   current tip onto the current bases rather
                                   than from whenever it was last made
                                   (--no-refresh-before-retry to skip it).
       * no CI / API error      -> appended to TO_MERGE_LATER.txt  (skipped)
       * green                  -> kept for merging
  2. Fetches the upstream remote (default: apache-github) and hard-resets each
     target branch (master, branch-4.x, branch-4.3, branch-4.2, branch-4.1,
     branch-3.5) to
     the upstream tip.  Local commits that would be discarded are first saved
     under refs/backup/merge_branches/<timestamp>/<branch>.
  3. Works out where each green branch is allowed to land:
       * a name ending in -master / -3.5 / -4.0 / -4.1 / -4.2 / -4.3 / -4.x / -4x
         goes to that one branch and nowhere else.  An optional "-branch" in
         front of the version, a revision marker after it, and a trailing -aok
         or -squashed (see --strip-suffix) are all ignored, so
         security-agg-injection-branch-4.2-aok goes to 4.2, and
         f091-kafka-datasource-option-restrict-4x,
         f091-kafka-datasource-option-restrict-branch-4x and
         f091-kafka-datasource-option-restrict-branch-4.x-r2 all go to 4.x;
       * otherwise, a branch cut from branch-3.5 goes to branch-3.5 only;
       * otherwise it goes to every target branch.
     A branch pinned to a release branch that is not in --targets is still
     handled: that branch is reset onto upstream on demand.
  4. Branches that end up going to branch-3.5 and nothing else are listed in
     3.5only.txt for review.
  5. Refuses to spread a multi-commit branch around: anything with more than one
     commit is listed in squash_me_with_your_feet_carl.txt and left alone.
  6. Checks whether each target already carries the branch (same patch id, an -x
     cherry-pick trailer, or the same subject *and* the same patch) and skips the
     ones that do, listing them in skipped.txt.  A commit that gets past that
     check and then turns out to change nothing -- the cherry-pick comes up
     empty because the work went in rewritten or squashed into something bigger
     -- is not a conflict: it is noted in already_landed.txt and the rest of the
     series carries on.  A whole branch whose work is already upstream, which is
     what an -aok that carries nothing means, goes in that same file.  A target holding a commit with
     the same subject but a different patch -- an earlier revision of the same
     work -- is flagged in NEEDS_REVIEW.txt and still attempted.  Then cherry-picks the branch onto each of
     its targets.  Anything that does not
     apply cleanly is aborted and recorded in rejects.txt.
  7. Shows you the branch's own diff and asks yes/no before anything is merged;
     a no goes to operator-rejected.txt with whatever reason you type.  A branch
     the ledger says you already approved is not shown again, so a later run that
     picks up a new target just gets on with it.
  8. Sanity-checks each applied series: if the claude CLI is alive it is asked
     for a verdict, and anything it calls QUESTIONABLE is put to the human.  If
     claude is not available the human is asked directly.  Series that are not
     approved are rolled back and listed in NEEDS_REVIEW.txt.
  9. Builds the target with sbt ("compile Test/compile", retried once as "clean
     compile Test/compile" in case the first failure was stale build state) and
     only pushes if that succeeds.  The build is skipped for the branch the
     work was cut from, since the fork's CI already ran there (--no-ci-shortcut).  A failed build is rolled back and recorded in
     COMPILE_FAILED.txt, keeping the full log, the extracted errors and a ref
     pointing at the tree that failed so it can be checked out later.
 10. Pushes to the push remote (default: apache) and notes the merge in
     branches-already-merged.csv, which a later run reads to leave it alone.  master is done first and the
     release branches are picked from the commit that landed there, so their
     "(cherry picked from commit ...)" trailer names a sha that exists in
     apache/spark rather than a local one.

Branches are audited and put to you for approval first, then merged; -j N merges
N of them at once, each in its own worktree under .merge_worktrees/ (kept between
runs for their sbt state), so a long batch can be left running unattended.

Pushing is OFF by default -- everything is done locally and the pushes are only
printed -- pass --push to actually publish.

branch-3.5 and master do not want the same JDK; point one build at one JDK with
e.g. SBT_JAVA_HOME_branch_3_5=/usr/lib/jvm/java-17-openjdk-amd64 ./merge_branches.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import itertools
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Branches always considered when working out where a topic branch was cut from,
# even when they are not merge targets for this run.
BASE_CANDIDATES = [
    "branch-4.x", "branch-4.3", "branch-4.2", "branch-4.1", "branch-4.0", "branch-3.5",
]
DEFAULT_TARGETS = ["master", "branch-4.x", "branch-4.3", "branch-4.2", "branch-4.1",
                   "branch-3.5"]

# A conclusion that means the commit is not green.
BAD_CONCLUSIONS = {
    "failure", "timed_out", "cancelled", "startup_failure", "action_required", "error",
}
GOOD_CONCLUSIONS = {"success", "neutral"}


# --------------------------------------------------------------------- logging
_worker = threading.local()
_printing = threading.Lock()


def worker_tag(tag: str) -> None:
    """Label this thread's output, so parallel workers stay readable."""
    _worker.tag = tag


def _tag() -> str:
    return getattr(_worker, "tag", "")


def log(msg: str) -> None:
    with _printing:
        print(f"{time.strftime('%H:%M:%S')} {_tag()}{msg}", flush=True)


def warn(msg: str) -> None:
    with _printing:
        print(f"{time.strftime('%H:%M:%S')} {_tag()}!! {msg}", file=sys.stderr, flush=True)


class Bail(Exception):
    """Fatal, reported without a traceback."""


# ----------------------------------------------------------------------- rules
# These are pure functions so they can be tested without a repository.

# Squashed rewrites of a branch keep the original name with a marker on the end;
# the marker is not part of the routing decision, so take it off first.
STRIP_SUFFIXES = ["-aok", "-squashed"]

SUFFIX_TARGETS = [
    ("-master", "master"),
    ("-3.5", "branch-3.5"),
    ("-4.0", "branch-4.0"),
    ("-4.1", "branch-4.1"),
    ("-4.2", "branch-4.2"),
    ("-4.3", "branch-4.3"),
    ("-4.x", "branch-4.x"),
    ("-4x", "branch-4.x"),
]

TARGET_BY_VERSION = {suffix[1:]: target for suffix, target in SUFFIX_TARGETS}

# The pin is written several ways for the same release: -4.x and -4x, either of
# them with or without a "-branch" in front, and any of those may carry the -r2
# of a redone cut.  They all say the same thing, so read them with one pattern
# instead of a list of spellings -- a spelling missing from such a list reads as
# an unpinned branch and would go to every release.
VERSION_SUFFIX = re.compile(
    "-(?:branch-)?(" + "|".join(re.escape(v) for v in TARGET_BY_VERSION) + r")(?:-r\d+)?$"
)


def logical_name(branch: str, strip: list[str] | None = None) -> str:
    """The branch name with its bookkeeping suffixes removed.

    security-agg-injection-branch-4.2-aok -> security-agg-injection-branch-4.2
    """
    strip = STRIP_SUFFIXES if strip is None else strip
    changed = True
    while changed:
        changed = False
        for suffix in strip:
            if suffix and branch.endswith(suffix) and len(branch) > len(suffix):
                branch = branch[: -len(suffix)]
                changed = True
    return branch


def suffix_target(branch: str, strip: list[str] | None = None) -> str | None:
    """A trailing version in the name pins a branch to that release branch alone."""
    found = VERSION_SUFFIX.search(logical_name(branch, strip))
    return TARGET_BY_VERSION[found.group(1)] if found else None


def family_stem(branch: str, strip: list[str] | None = None) -> str:
    """The name with its markers and any trailing version taken off.

    foo-3.5-aok and foo-aok are both "foo": two cuts of the same work.  The
    "-branch" of foo-branch-3.5 and the "-r2" of foo-3.5-r2 belong to the
    version, not to the name, or those cuts would look like a different piece
    of work from the plain branch they belong to.
    """
    base = logical_name(branch, strip)
    return VERSION_SUFFIX.sub("", base) or base


def sibling_coverage(branch: str, targets: list[str], all_branches: list[str],
                     strip: list[str] | None = None) -> dict[str, str]:
    """target -> the sibling branch that owns it.

    A change that would not apply to one release usually arrives as a pair: the
    plain branch, and a -3.5 (or -4.1, ...) cut of the same work.  The plain one
    should leave that release to its sibling instead of conflicting on it.
    """
    if suffix_target(branch, strip):        # already pinned; it owns nothing else
        return {}
    stem = family_stem(branch, strip)
    covered = {}
    for other in all_branches:
        if other == branch:
            continue
        pinned = suffix_target(other, strip)
        if pinned and pinned in targets and family_stem(other, strip) == stem:
            covered[pinned] = other
    return covered


def targets_for(branch: str, base_branch: str, default_targets: list[str],
                strip: list[str] | None = None) -> list[str]:
    """Where this branch is allowed to land."""
    pinned = suffix_target(branch, strip)
    if pinned:
        return [pinned]
    # Cut from branch-3.5: 3.5 has diverged far enough that these are 3.5-only.
    if base_branch == "branch-3.5":
        return ["branch-3.5"]
    return list(default_targets)


def ci_status(runs: list[tuple[str, str]]) -> str:
    """Fold (status, conclusion) pairs into running / failing / passing / none."""
    if not runs:
        return "none"
    if any(status != "completed" for status, _ in runs):
        # Still building is "come back later", not "rejected" -- but say so if
        # something has already failed, or it looks like a clean pending run.
        if any(conclusion in BAD_CONCLUSIONS for _, conclusion in runs):
            return "running-with-failures"
        return "running"
    if any(conclusion in BAD_CONCLUSIONS for _, conclusion in runs):
        return "failing"
    if any(conclusion in GOOD_CONCLUSIONS for _, conclusion in runs):
        return "passing"
    return "none"  # everything skipped


def parse_branch_file(text: str) -> list[tuple[str, str | None]]:
    """Lines are "<branch>" or "<branch> <base-ref>"; "#" starts a comment."""
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0] in seen:          # listed twice: merging it twice is not a plan
            continue
        seen.add(parts[0])
        out.append((parts[0], parts[1] if len(parts) > 1 else None))
    return out


def squash_output(text: str, strip: list[str] | None = None) -> dict[str, str]:
    """logical branch name -> the rewrite squash-magic.sh just cut for it.

    Its output file is one branch name per line, the same shape as the branch
    file.  A branch that came back with a different marker than the one in the
    branch file -- -squashed where it used to be -aok, because a second commit
    turned up on the fork -- is found through the logical name they share.
    """
    return {logical_name(name, strip): name for name, _ in parse_branch_file(text)}


VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(OK|QUESTIONABLE)\b[\s\-:]*(.*)$",
                        re.IGNORECASE | re.MULTILINE)


def parse_verdict(text: str) -> tuple[str, str]:
    """-> ("OK"|"QUESTIONABLE"|"UNKNOWN", reason)."""
    if not text.strip():
        return "UNKNOWN", "claude gave no answer"
    match = VERDICT_RE.search(text)
    if not match:
        squashed = " ".join(text.split())[:200]
        return "UNKNOWN", f"could not parse: {squashed}"
    return match.group(1).upper(), match.group(2).strip()


# A subject has to carry some information before "the target already has a commit
# with this subject" means anything -- "[MINOR] Fix typo" does not.
MIN_SUBJECT_LEN = 25


def is_specific_subject(subject: str) -> bool:
    return len(subject.strip()) >= MIN_SUBJECT_LEN


def ref_slug(text: str) -> str:
    """Safe for a ref component and a file name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)


# ------------------------------------------------------------------- reporting
# Worker checkouts live here; they are kept between runs for their build state.
WORKTREE_DIR = ".merge_worktrees"

# Branches you want left alone.  Written by you, read by this script -- unlike
# TO_MERGE_LATER.txt, which is written by this script and read by you.
HOLD_FILE = "HELD_BRANCHES.txt"

# Diffs you have already looked at and said yes to.  Keyed by the patch id of the
# change, not the branch name: if the branch is pushed again with different
# content, it is a different diff and you get asked again.
APPROVALS_FILE = "approved_diffs.txt"
APPROVAL_COLUMNS = ["approved_at", "branch", "head", "patch_id", "targets"]


def load_approvals(path: Path) -> set[tuple[str, str]]:
    """The (branch, patch id) pairs already approved.

    A plain report file like the others: "#" lines are the run stamp and the
    column names, everything else is a tab separated row.
    """
    if not path.is_file():
        return set()
    branch_at, patch_at = APPROVAL_COLUMNS.index("branch"), APPROVAL_COLUMNS.index("patch_id")
    approved = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) <= patch_at:
            continue
        branch, patch_id = fields[branch_at].strip(), fields[patch_at].strip()
        if branch and patch_id:
            approved.add((branch, patch_id))
    return approved


def load_hold_list(path: Path) -> dict[str, str]:
    """branch -> why it is being held, from HELD_BRANCHES.txt.

    Yours to maintain: one branch name per line, anything after a tab is treated
    as the reason and echoed back at you.  Nothing writes to it, so a branch stays
    held until you take it out.  (TO_MERGE_LATER.txt is the opposite: a report the
    script writes and never reads.)
    """
    if not path.is_file():
        return {}
    held: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = [field.strip() for field in line.split("\t")]
        branch = fields[0]
        if branch:
            held.setdefault(branch, fields[-1] if len(fields) > 1 else "listed")
    return held


def load_ledger_commits(path: Path) -> dict[tuple[str, str], str]:
    """(branch, target) -> the commit this script created there."""
    if not path.is_file():
        return {}
    landed = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            branch, target = (row.get("branch") or "").strip(), (row.get("target") or "").strip()
            commit = (row.get("commit") or "").strip()
            if branch and target and commit:
                landed[(branch, target)] = commit
    return landed


def load_ledger_heads(path: Path) -> set[tuple[str, str]]:
    """(branch, source head) pairs from the ledger, for merges made before
    approvals were recorded separately."""
    if not path.is_file():
        return set()
    heads = set()
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            branch, head = (row.get("branch") or "").strip(), (row.get("source_head") or "").strip()
            if branch and head:
                heads.add((branch, head))
    return heads


# Every merge that actually reached the push remote, so a rerun does not redo it.
LEDGER_FILE = "branches-already-merged.csv"
LEDGER_COLUMNS = ["merged_at", "branch", "target", "commit", "source_head", "pushed_to"]


def load_ledger(path: Path) -> set[tuple[str, str]]:
    """The (branch, target) pairs that have already been merged and pushed."""
    if not path.is_file():
        return set()
    done = set()
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            branch, target = (row.get("branch") or "").strip(), (row.get("target") or "").strip()
            if branch and target:
                done.add((branch, target))
    return done


# A branch with more than one commit is not cherry-picked; it goes here to be
# squashed first.
SQUASH_FILE = "squash_me_with_your_feet_carl.txt"

# The repo's own helpers.  The local -aok/-squashed rewrites are cut against the
# base branches as they were when somebody ran squash-magic.sh, so before the
# last look at a red branch both are run again: the bases have moved on, and the
# branch itself may have been rebased or had a fix pushed to it since.
UPDATE_BASES_SCRIPT = Path("update-bases.sh")
SQUASH_SCRIPT = Path("squash-magic.sh")

# Written under the "# run" line the first time a file is touched, so a row on
# its own still says which branch is which.
REPORT_COLUMNS = {
    "TO_MERGE_LATER.txt": ["branch", "head-on-fork", "why"],
    "FAILING_CI.txt": ["branch", "head-on-fork", "url"],
    "rejects.txt": ["source-branch", "rejected-on-branch", "commit", "subject",
                    "picked-from", "why"],
    "NEEDS_REVIEW.txt": ["source-branch", "target-branch", "why", "detail"],
    "COMPILE_FAILED.txt": ["source-branch", "failed-on-branch", "build-log", "kept-ref",
                           "first-error"],
    "skipped.txt": ["source-branch", "target-branch", "why"],
    # Everything already upstream: whole branches whose work has landed (target
    # "-"), and single commits a cherry-pick found nothing left to apply for.
    "already_landed.txt": ["source-branch", "target-branch", "commit", "subject", "why"],
    "3.5only.txt": ["branch", "head", "why", "url"],
    SQUASH_FILE: ["branch", "commits", "head", "picked-from", "url"],
    "operator-rejected.txt": ["branch", "head", "picked-from", "url", "reason"],
    APPROVALS_FILE: APPROVAL_COLUMNS,
}


class Reports:
    """Append-only report files, stamped the first time each one is written to.

    In a dry run nothing is written -- each row is logged instead.
    """

    def __init__(self, stamp: str, directory: Path, dry_run: bool = False):
        self.stamp = stamp
        self.dir = directory
        self.dry_run = dry_run
        self.stamped: set[Path] = set()
        self.lock = threading.Lock()

    def write(self, name: str, *columns: object) -> None:
        self.write_to(self.dir / name, REPORT_COLUMNS.get(name), *columns)

    def write_to(self, path: Path, header: list[str] | None, *columns: object) -> None:
        """Same, but to a path the caller chose (so --approvals is honoured)."""
        row = "\t".join(str(c) for c in columns)
        if self.dry_run:
            log(f"  [dry-run] would add to {path.name}: {row}")
            return
        with self.lock, path.open("a", encoding="utf-8") as handle:
            if path not in self.stamped:
                handle.write(f"# run {self.stamp}\n")
                if header:
                    handle.write("# " + "\t".join(header) + "\n")
                self.stamped.add(path)
            handle.write(row + "\n")


# ------------------------------------------------------------------------- git
class Git:
    def __init__(self, root: Path):
        self.root = root
        self.last_stderr = ""

    def why(self, limit: int = 200) -> str:
        """The line that says what went wrong, not git's progress chatter."""
        lines = [line.strip() for line in self.last_stderr.splitlines() if line.strip()]
        for line in lines:
            if line.startswith(("fatal:", "error:")) or line.startswith("! ["):
                return line[:limit]
        return lines[0][:limit] if lines else "no output"

    def run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["git", *args], cwd=self.root, check=check, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.last_stderr = (result.stderr or "").strip()
        return result

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    def ok(self, *args: str) -> bool:
        return self.run(*args).returncode == 0

    def lines(self, *args: str) -> list[str]:
        return [line for line in self.run(*args).stdout.splitlines() if line.strip()]

    def pipe(self, args: list[str], stdin_text: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, input=stdin_text, text=True,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL).stdout.strip()

    def rev_parse(self, rev: str) -> str | None:
        result = self.run("rev-parse", "--verify", "--quiet", rev)
        return result.stdout.strip() or None

    def has_ref(self, ref: str) -> bool:
        return self.rev_parse(ref) is not None

    def subject(self, rev: str) -> str:
        return self.out("log", "-1", "--format=%s", rev)

    def short(self, rev: str) -> str:
        return self.out("rev-parse", "--short", rev)


# ------------------------------------------------------------------ the driver
@dataclass
class Config:
    branch_file: Path
    targets: list[str]
    fork_repo: str
    fork_remote: str
    upstream: str
    push_remote: str
    do_push: bool
    skip_ci: bool
    sanity: bool
    review_model: str
    review_timeout: int
    max_diff_bytes: int
    assume_yes: bool
    max_commits: int
    strip_suffixes: list[str]
    pick_from_fork: bool
    operator_review: bool
    ci_shortcut: bool
    review_diff_lines: int
    do_compile: bool
    dry_run: bool
    sbt: Path | None          # None: use each worktree's own build/sbt
    sbt_tasks: list[str]
    clean_retry: bool
    sbt_timeout: int
    log_dir: Path
    ledger: Path
    approvals: Path
    ignore_ledger: bool
    hold_list: Path
    ignore_hold_list: bool
    jobs: int
    ci_poll_minutes: float
    ci_wait_hours: float
    refresh_before_retry: bool
    local_stand_in: bool
    update_bases: Path
    squash_magic: Path
    refresh_timeout: int


@dataclass
class CompileFailure:
    log: Path
    kept_ref: str
    first_error: str


@dataclass
class Source:
    name: str                      # as written in the branch file
    explicit_base: str | None = None
    fork_name: str = ""            # the branch on the fork, whose CI we trust
    fork_head: str = ""
    pick_name: str = ""            # the ref the commits actually come from
    head: str = ""                 # ...and its tip
    ci_green: bool = False         # GitHub says the fork branch is green
    ci_covers_change: bool = False # ...and it ran on the change we are picking
    base: str = ""
    base_branch: str = ""
    commits: list[str] = field(default_factory=list)


class Backporter:
    def __init__(self, cfg: Config, git: Git, reports: Reports, stamp: str):
        self.cfg = cfg
        self.git = git
        self.reports = reports
        self.stamp = stamp
        self.tag = ""
        # Git state, per worktree -- not shared with the workers.
        self.ready: set[str] = set()      # targets already put on the upstream tip
        self.valid_targets: list[str] = []
        self.detached = False             # workers run on a detached HEAD
        # The record of the run -- shared with the workers.
        self.outcomes: list[tuple[str, str, str]] = []   # (source, target, what happened)
        self.listed: list[str] = []       # every branch name in this run
        self.claude_alive = False
        self.ledger_lock = threading.Lock()
        # Workers all push to the same handful of branches; queueing per target
        # turns a scramble into an orderly line and saves needless rebases.
        self.push_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        # What has already been merged, and what you have already said yes to.
        # Both anchored to content: the approvals file by patch id, the ledger by
        # the head it merged from, so a re-pushed branch is asked about again.
        self.merged = set() if cfg.ignore_ledger else load_ledger(cfg.ledger)
        self.approvals = set() if cfg.ignore_ledger else load_approvals(cfg.approvals)
        self.merged_heads = set() if cfg.ignore_ledger else load_ledger_heads(cfg.ledger)
        self.ledger_commits = load_ledger_commits(cfg.ledger)
        # Written by earlier runs, and by you: either way, hands off.
        self.held = {} if cfg.ignore_hold_list else load_hold_list(cfg.hold_list)

    def already_approved(self, src: Source) -> bool:
        """Have you seen this exact change before and said yes?"""
        if (src.name, src.head) in self.merged_heads:
            return True
        signature = self.change_signature(src.head)
        return bool(signature) and (src.name, signature) in self.approvals

    def remember_approval(self, src: Source, targets: list[str]) -> None:
        signature = self.change_signature(src.head) or ""
        if self.cfg.dry_run:
            log(f"  [dry-run] would record approval of {src.name} in {self.cfg.approvals}")
            return
        self.reports.write_to(self.cfg.approvals, APPROVAL_COLUMNS,
                              datetime.now().astimezone().isoformat(timespec="seconds"),
                              src.name, src.head, signature, " ".join(targets))
        self.approvals.add((src.name, signature))

    def for_worktree(self, path: Path, tag: str) -> "Backporter":
        """A twin of this Backporter working in its own worktree.

        Everything that is a record of the run -- reports, outcomes, the ledger --
        is shared; everything that is git state is not.
        """
        twin = Backporter.__new__(Backporter)
        twin.__dict__.update(self.__dict__)
        twin.git = Git(path)
        twin.ready = set()
        twin.detached = True
        twin.tag = tag
        return twin

    def record(self, src: str, target: str, outcome: str) -> None:
        self.outcomes.append((src, target, outcome))

    def summary(self) -> None:
        if not self.outcomes:
            log("nothing to summarise")
            return
        log("would do:" if self.cfg.dry_run else "did:")
        grouped: dict[str, list[str]] = {}
        for src, target, outcome in self.outcomes:
            grouped.setdefault(outcome, []).append(f"{src} -> {target}" if target != "-" else src)
        for outcome, pairs in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
            log(f"  {outcome} ({len(pairs)})")
            for pair in pairs:
                log(f"      {pair}")

    # -- finding the branch ------------------------------------------------
    def resolve_on_fork(self, listed: str) -> tuple[str, str] | None:
        """-> (branch name on the fork, head sha).

        The branch file may carry a marker the fork does not, e.g.
        f11-parquet-eager-alloc-aok is f11-parquet-eager-alloc over there.
        """
        candidates = dict.fromkeys(
            [listed, logical_name(listed, self.cfg.strip_suffixes)])
        for candidate in candidates:
            head = self.git.rev_parse(
                f"refs/remotes/{self.cfg.fork_remote}/{candidate}^{{commit}}")
            if head:
                return candidate, head
        return None

    def resolve_pick_ref(self, src: Source) -> tuple[str, str]:
        """Where the commits come from: the local branch under the listed name.

        The -aok and -squashed branches are rewrites that live locally only, so
        the commits come from there while CI was run on the fork's copy.
        """
        if not self.cfg.pick_from_fork:
            local = self.git.rev_parse(f"refs/heads/{src.name}^{{commit}}")
            if local:
                return src.name, local
        return f"{self.cfg.fork_remote}/{src.fork_name}", src.fork_head

    def change_signature(self, head: str) -> str | None:
        """patch-id of everything the branch changes, against its own base.

        Squashing and rebasing both leave this alone, so it compares a local -aok
        rewrite against the fork branch CI actually ran on.
        """
        found = self.best_base(head)
        if not isinstance(found, tuple):
            return None
        base, _ = found
        diff = self.git.out("diff", f"{base}..{head}")
        if not diff:
            return None
        signature = self.git.pipe(["patch-id", "--stable"], diff + "\n")
        return signature.split()[0] if signature else None

    def carries_nothing(self, head: str) -> bool:
        """True when the ref has nothing of its own -- its base holds it all.

        A rewrite lands here when the work is already upstream: the rebase that
        cut it dropped every commit as applied and left the branch sitting on
        the base.  plan() skips it a moment later, for the right reason; this is
        so warn_if_untested does not call it a CI problem on the way past.
        """
        found = self.best_base(head)
        if not isinstance(found, tuple):
            return False
        base, _ = found
        return not self.git.out("diff", f"{base}..{head}")

    def warn_if_untested(self, src: Source) -> None:
        """CI ran on the fork branch; say so if what we are picking differs.

        A rebase is not a difference: the rewrite is cut from the fork branch, so
        the patch id is what decides, not the sha.  Same patch, same change, and
        the green tick on the fork covers it.
        """
        if src.head == src.fork_head:
            src.ci_covers_change = True
            return
        picked, tested = self.change_signature(src.head), self.change_signature(src.fork_head)
        if picked is not None and picked == tested:
            src.ci_covers_change = True
            log(f"  {src.name}: picking {src.pick_name} ({src.head[:9]}) -- same change as "
                f"{src.fork_name} ({src.fork_head[:9]}), which is what CI ran on")
        elif picked is None and self.carries_nothing(src.head):
            # Not "CI covers something else" -- there is nothing here to cover.
            log(f"  {src.name}: {src.pick_name} ({src.head[:9]}) carries nothing of its "
                "own; the work is already on its base, so there is nothing to merge")
        else:
            warn(f"  {src.name}: picking {src.pick_name} ({src.head[:9]}) but CI ran on "
                 f"{src.fork_name} ({src.fork_head[:9]}) and the change is not the same -- "
                 "the green tick does not cover what is being merged")
            self.reports.write("NEEDS_REVIEW.txt", src.name, "-", "CI_COVERS_A_DIFFERENT_CHANGE",
                               f"picked {src.pick_name}@{src.head[:9]}, "
                               f"CI ran on {src.fork_name}@{src.fork_head[:9]}")

    # -- CI ----------------------------------------------------------------
    def _gh_lines(self, path: str, jq: str) -> list[str] | None:
        result = subprocess.run(
            ["gh", "api", "--paginate", path, "--jq", jq],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return None
        return [line for line in result.stdout.splitlines() if line.strip()]

    def ci_status_of(self, sha: str) -> str:
        repo = self.cfg.fork_repo
        checks = self._gh_lines(f"repos/{repo}/commits/{sha}/check-runs",
                                ".check_runs[] | @json")
        if checks is None:
            return "error"
        runs: list[tuple[str, str]] = []
        for line in checks:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            runs.append((item.get("status") or "", item.get("conclusion") or ""))
        # Some jobs report through the older commit-statuses API instead.
        statuses = self._gh_lines(f"repos/{repo}/commits/{sha}/status",
                                  ".statuses[] | @json") or []
        for line in statuses:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            state = item.get("state") or ""
            runs.append(("in_progress", "") if state == "pending" else ("completed", state))
        return ci_status(runs)

    # -- targets -----------------------------------------------------------
    def use_target(self, target: str) -> bool:
        """Put HEAD on `target`, ready to be picked onto."""
        if self.detached:
            # A worktree cannot check out a branch the main tree holds, and it has
            # no local branches of its own: start each target from the upstream tip
            # and let the push handle any race.
            return self.git.ok("checkout", "--detach", f"{self.cfg.upstream}/{target}")
        return self.git.ok("checkout", target)

    def ensure_target(self, target: str) -> bool:
        """Point `target` at the upstream tip, keeping anything local first."""
        if target in self.ready:
            return True
        upstream_ref = f"refs/remotes/{self.cfg.upstream}/{target}"
        if not self.git.has_ref(upstream_ref):
            warn(f"target {target} does not exist on {self.cfg.upstream}")
            return False
        if self.detached:
            self.ready.add(target)     # nothing to reset; use_target detaches
            return True
        old = self.git.rev_parse(f"refs/heads/{target}")
        if old:
            ahead = self.git.out("rev-list", "--count", f"{self.cfg.upstream}/{target}..{old}")
            if ahead and ahead != "0":
                backup = f"refs/backup/merge_branches/{self.stamp}/{target}"
                self.git.run("update-ref", backup, old, check=True)
                warn(f"{target} has {ahead} local commit(s) not on {self.cfg.upstream} "
                     f"-- kept as {backup}")
        if not self.git.ok("checkout", "-B", target, f"{self.cfg.upstream}/{target}"):
            warn(f"could not reset {target}: {self.git.why()}")
            return False
        log(f"{target} reset to {self.cfg.upstream}/{target} "
            f"({self.git.short('HEAD')}); was {old or '<new>'}")
        self.ready.add(target)
        return True

    # -- base --------------------------------------------------------------
    def pick_base(self, src: Source) -> tuple[str, str] | str | None:
        """-> (merge-base, branch it was cut from), or "merged:<branch>", or None."""
        if src.explicit_base:
            # Upstream first: a local branch of the same name may be months old.
            ref = src.explicit_base
            if self.git.has_ref(f"refs/remotes/{self.cfg.upstream}/{ref}"):
                ref = f"{self.cfg.upstream}/{ref}"
            elif not self.git.has_ref(f"{ref}^{{commit}}"):
                return None
            merge_base = self.git.out("merge-base", ref, src.head)
            return (merge_base, ref.rsplit("/", 1)[-1]) if merge_base else None

        return self.best_base(src.head)

    def best_base(self, head: str) -> tuple[str, str] | str | None:
        """The upstream branch this commit sits closest to, and the merge base."""
        best: tuple[str, str] | None = None
        best_count = -1
        seen: set[str] = set()
        for candidate in ["master", *BASE_CANDIDATES, *self.cfg.targets]:
            if candidate in seen:
                continue
            seen.add(candidate)
            if not self.git.has_ref(f"refs/remotes/{self.cfg.upstream}/{candidate}"):
                continue
            merge_base = self.git.out("merge-base", f"{self.cfg.upstream}/{candidate}", head)
            if not merge_base:
                continue
            counted = self.git.run("rev-list", "--count", f"{merge_base}..{head}")
            if counted.returncode != 0 or not counted.stdout.strip().isdigit():
                continue      # a git error is not the same as "nothing to pick"
            count = int(counted.stdout.strip())
            # count == 0 means the branch is already merged into that upstream
            # branch, so there is no range to derive -- any other base would drag
            # in everything that branch has accumulated since.  Ask for a pin.
            if count == 0:
                return f"merged:{candidate}"
            if best_count < 0 or count < best_count:
                best_count, best = count, (merge_base, candidate)
        return best

    # -- has this already landed? -----------------------------------------
    def commit_patch_id(self, commit: str) -> str | None:
        patch = self.git.out("show", "--format=", "--patch", commit)
        if not patch:
            return None
        signature = self.git.pipe(["patch-id", "--stable"], patch + "\n")
        return signature.split()[0] if signature else None

    def already_landed(self, target: str, src: Source) -> tuple[str, str] | None:
        """('skip' | 'flag', reason) for what `target` already has, or None.

        Checked against the upstream tip rather than the local branch, so it
        answers "is this already in apache?" and not "did this run apply it?".
        """
        upstream_ref = f"{self.cfg.upstream}/{target}"
        if not self.git.has_ref(f"refs/remotes/{upstream_ref}"):
            return None
        span = f"{src.base}..{upstream_ref}"

        # 1. Same patch, by patch id -- what git cherry-pick itself would notice.
        marks = self.git.lines("cherry", upstream_ref, src.head, src.base)
        if marks and all(line.startswith("-") for line in marks):
            return "skip", "identical patch already there"

        # 2. The -x trailer this script leaves behind on a backport.
        # Either sha may be the one recorded: this script now records the commit as
        # it landed on master, older backports recorded the topic branch's.
        # -x records the master-landed sha these days, the topic branch's before
        # that; look for whichever this change might have been recorded as.
        spellings = {src.fork_head, *src.commits, *self.on_master_already(src)}
        remembered = self.ledger_commits.get((src.name, "master"))
        if remembered:
            spellings.add(remembered)
        trailers = [
            commit for commit in src.commits
            if any(self.git.lines("log", "--format=%H", "--fixed-strings",
                                  f"--grep=cherry picked from commit {sha}", span)
                   for sha in spellings)
        ]
        if trailers and len(trailers) == len(src.commits):
            return "skip", "already cherry-picked (-x trailer)"

        # 3. Same subject.  A matching title is not enough on its own: the target
        #    may be carrying an *earlier revision* of the same work, and skipping
        #    would then drop the newer one.  Only call it landed when the patch
        #    matches too; otherwise hand it to the human.
        covered, stale = [], []
        for commit in src.commits:
            subject = self.git.subject(commit)
            if not is_specific_subject(subject):
                continue
            twins = self.git.lines("log", "--format=%H", "--fixed-strings",
                                   f"--grep={subject}", span)
            if not twins:
                continue
            mine = self.commit_patch_id(commit)
            if any(mine and mine == self.commit_patch_id(twin) for twin in twins):
                covered.append(commit)
            else:
                stale.append((commit, twins[0], subject))

        if covered and len(covered) == len(src.commits):
            return "skip", "same change already there under a different commit"
        if stale:
            commit, twin, subject = stale[0]
            return "flag", (f"{target} has {twin[:9]} with the same subject "
                            f"(\"{subject[:60]}\") but a different patch -- an earlier "
                            "revision of this work?")
        return None

    # -- the review gate ---------------------------------------------------
    def claude_ping(self) -> None:
        if not shutil.which("claude"):
            log("claude CLI not installed -- reviews go to you")
            return
        answer = self._claude("Reply with exactly one word: alive", timeout=90)
        if answer and answer.strip().lower() == "alive":
            self.claude_alive = True
            log(f"claude is alive ({self.cfg.review_model}) -- it will sanity-check each series first")
        else:
            log("claude did not answer -- reviews go to you")

    def _claude(self, prompt: str, timeout: int) -> str | None:
        try:
            result = subprocess.run(
                ["claude", "-p", "--model", self.cfg.review_model, "--allowed-tools", ""],
                input=prompt, text=True, timeout=timeout,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        return result.stdout if result.returncode == 0 else None

    def claude_verdict(self, src: str, target: str, rng: str) -> tuple[str, str]:
        # A second pair of eyes, not a security control: the diff being reviewed
        # is itself part of the prompt, so treat a lone OK as advice, not proof.
        diff = self.git.out("diff", rng)[: self.cfg.max_diff_bytes]
        prompt = f"""You are sanity-checking an Apache Spark backport before it is pushed to the
official apache/spark repository. The commits below were cherry-picked from the
branch '{src}' onto the release branch '{target}' and applied without conflicts.

Reply with exactly one line, nothing else, in one of these two forms:
VERDICT: OK
VERDICT: QUESTIONABLE - <short reason>

Say QUESTIONABLE if the change looks wrong for this branch, for example: it uses
APIs, config keys or syntax that may not exist on '{target}'; it is far larger than
a normal backport; it touches version/release metadata; it contains debug
leftovers, secrets or commented-out code; it deletes tests; or the cherry-pick
looks semantically wrong even though it applied cleanly. Otherwise say OK.

Commits:
{self.git.out("log", "--format=%h %s", rng)}

Diffstat:
{self.git.out("diff", "--stat", rng)}

Diff (may be truncated):
{diff}"""
        answer = self._claude(prompt, timeout=self.cfg.review_timeout)
        if answer is None:
            return "UNKNOWN", "claude gave no answer"
        return parse_verdict(answer)

    @staticmethod
    def open_tty():
        """-> (reader, writer) on the terminal, or None.

        /dev/tty exists even with no controlling terminal, so it has to be opened
        to find out.  Two one-way handles: "r+" on a tty is not seekable, which
        buffered random access needs.
        """
        try:
            return open("/dev/tty", "r"), open("/dev/tty", "w")
        except OSError:
            return None

    def operator_review(self, members: list[tuple[Source, list[str]]]) -> bool:
        """Show one diff for a family of branches and ask once.

        `members` are cuts of the same work -- the plain branch and its -3.5 /
        -4.1 / ... siblings.  They differ only in what each release needed, so
        one diff is the question and [a] shows the other cuts.
        """
        if not self.cfg.operator_review or self.cfg.assume_yes:
            return True

        names = ", ".join(src.name for src, _ in members)
        if self.cfg.dry_run:
            log(f"  [dry-run] would show you the diff for {names} and ask before merging")
            return True

        def reject(reason: str) -> None:
            for src, _ in members:
                self.reports.write("operator-rejected.txt", src.name, src.head,
                                   src.pick_name,
                                   f"https://github.com/{self.cfg.fork_repo}/tree/{src.fork_name}",
                                   reason)
                self.record(src.name, "-", "rejected by operator")

        handles = self.open_tty()
        if handles is None:
            warn(f"  {names}: no terminal to review on -- not merging")
            reject("no terminal to ask on")
            return False

        # Show a cut you have not already approved, else the one going furthest.
        order = sorted(members, key=lambda m: (self.already_approved(m[0]), -len(m[1])))
        shown_src, shown_targets = order[0]
        others = [m for m in order[1:]]

        tty_in, tty = handles
        with tty_in, tty:
            def write_diff(src: Source, targets: list[str], full: bool) -> None:
                span = f"{src.base}..{src.head}"
                lines = self.git.out("diff", span).splitlines()
                tty.write(f"\n--- {src.name}  ({src.pick_name} {src.head[:9]}) "
                          f"-> {' '.join(targets)}\n")
                tty.write(f"    https://github.com/{self.cfg.fork_repo}/tree/{src.fork_name}\n\n")
                tty.write(self.git.out("log", "--format=%h %s%n%b", span) + "\n")
                tty.write(self.git.out("diff", "--stat", span) + "\n\n")
                cut = lines if full else lines[: self.cfg.review_diff_lines]
                tty.write("\n".join(cut) + "\n")
                if len(cut) < len(lines):
                    tty.write(f"\n... {len(lines) - len(cut)} more lines "
                              "-- press f for the whole diff\n")
                tty.flush()

            def header() -> None:
                tty.write("\n" + "=" * 78 + "\n")
                if len(members) > 1:
                    stem = family_stem(shown_src.name, self.cfg.strip_suffixes)
                    tty.write(f"{stem}: {len(members)} cuts of the same work\n")
                    for src, targets in order:
                        mark = "*" if src is shown_src else " "
                        seen = "  (approved before)" if self.already_approved(src) else ""
                        tty.write(f"  {mark} {src.name} -> {' '.join(targets)}{seen}\n")
                    tty.write("  (* is the one shown below; press a for the others)\n")

            header()
            write_diff(shown_src, shown_targets, full=False)
            question = (f"merge all {len(members)} cuts" if len(members) > 1
                        else f"merge {shown_src.name}")
            while True:
                extra = "   [a] the other cuts" if others else ""
                tty.write(f"\n{question}?  [y] yes   [n] no   "
                          f"[f] full diff   [s] stat only{extra} : ")
                tty.flush()
                answer = (tty_in.readline() or "n").strip().lower()
                if answer in ("y", "yes"):
                    log(f"  {names}: you approved it")
                    for src, targets in members:
                        self.remember_approval(src, targets)
                    return True
                if answer in ("", "n", "no"):
                    tty.write("reason (optional, enter to skip): ")
                    tty.flush()
                    reason = (tty_in.readline() or "").strip() or "rejected by operator"
                    warn(f"  {names}: rejected -- {reason}")
                    reject(reason)
                    return False
                if answer == "f":
                    write_diff(shown_src, shown_targets, full=True)
                elif answer == "s":
                    for src, _ in order:
                        tty.write(f"\n{src.name}:\n"
                                  + self.git.out("diff", "--stat", f"{src.base}..{src.head}")
                                  + "\n")
                    tty.flush()
                elif answer == "a" and others:
                    for src, targets in others:
                        write_diff(src, targets, full=False)
                else:
                    tty.write("answer y, n, f, s" + (", a" if others else "") + "\n")

    def ask_human(self, question: str, rng: str) -> bool:
        handles = self.open_tty()
        if handles is None:
            warn("  no terminal to ask on -- not pushing")
            return False
        tty_in, tty = handles
        with tty_in, tty:
            while True:
                tty.write(f"\n{question}\n"
                          "  [y] push   [n] skip   [d] show the diff   [s] show the log : ")
                tty.flush()
                line = tty_in.readline()
                if not line:
                    return False
                answer = line.strip().lower()
                if answer in ("y", "yes"):
                    return True
                if answer in ("", "n", "no"):
                    return False
                if answer == "d":
                    tty.write(self.git.out("--no-pager", "diff", rng) + "\n")
                elif answer == "s":
                    tty.write(self.git.out("--no-pager", "log", "--stat", rng) + "\n")
                else:
                    tty.write("answer y, n, d or s\n")

    def may_prompt(self) -> bool:
        """Whether this gate is allowed to stop and ask.

        It is not, once you have already approved the branch up front, nor with
        workers running in parallel -- the whole point is that the rest of the run
        needs nobody.  Anything doubtful goes to NEEDS_REVIEW.txt instead.
        """
        return not (self.cfg.assume_yes or self.cfg.dry_run
                    or self.cfg.operator_review or self.cfg.jobs > 1)

    def sanity_check(self, src: str, target: str, rng: str) -> bool:
        if not self.cfg.sanity:
            return True

        if not self.claude_alive:
            # You looked at this diff and approved it up front; claude was the
            # second pair of eyes, not the only one.  Missing it is worth saying
            # out loud, but it is not a reason to drop the whole run on the floor.
            if self.cfg.operator_review and not self.cfg.dry_run:
                warn(f"  {target}: claude unavailable -- going on your approval alone")
                return True
            if not self.may_prompt() and not self.cfg.dry_run:
                warn(f"  {target}: claude unavailable and nobody to ask -- not pushing")
                self.reports.write("NEEDS_REVIEW.txt", src, target, "NO_REVIEWER",
                                   "claude unavailable")
                return False
            if self.cfg.dry_run:
                log(f"  [dry-run] would ask you to eyeball {src} -> {target} "
                    "(claude is not available)")
                self.reports.write("NEEDS_REVIEW.txt", src, target, "NO_REVIEWER",
                                   "claude unavailable")
                return False
            if self.cfg.assume_yes:
                warn(f"  {target}: claude unavailable and --assume-yes is only for clean "
                     "verdicts -- not pushing")
                self.reports.write("NEEDS_REVIEW.txt", src, target, "NO_REVIEWER",
                                   "claude unavailable")
                return False
            if self.ask_human(f"{src} -> {target}: claude is not available, please eyeball "
                              "this backport.\nPush it?", rng):
                return True
            self.reports.write("NEEDS_REVIEW.txt", src, target, "NOT_APPROVED",
                               "declined by human")
            return False

        verdict, reason = self.claude_verdict(src, target, rng)
        if verdict == "OK":
            log(f"  {target}: claude says the {src} backport looks OK")
            return True

        if verdict == "QUESTIONABLE":
            warn(f"  {target}: claude flagged the {src} backport: {reason}")
            if self.cfg.dry_run or not self.may_prompt():
                if self.cfg.dry_run:
                    log(f"  [dry-run] would stop and ask you about {src} -> {target}")
                self.reports.write("NEEDS_REVIEW.txt", src, target, "QUESTIONABLE", reason)
                return False
            if self.cfg.assume_yes:
                self.reports.write("NEEDS_REVIEW.txt", src, target, "QUESTIONABLE", reason)
                return False
            if self.ask_human(f"{src} -> {target}: claude says QUESTIONABLE: {reason}\n"
                              "Push it anyway?", rng):
                log(f"  {target}: you approved it")
                return True
            self.reports.write("NEEDS_REVIEW.txt", src, target, "QUESTIONABLE", reason)
            return False

        warn(f"  {target}: no usable verdict from claude ({reason})")
        if self.cfg.dry_run or not self.may_prompt():
            if self.cfg.dry_run:
                log(f"  [dry-run] would stop and ask you about {src} -> {target}")
        elif self.cfg.assume_yes:
            warn(f"  {target}: --assume-yes only covers a clean OK -- not pushing")
        elif self.ask_human(f"{src} -> {target}: claude could not review this ({reason}).\n"
                            "Push it anyway?", rng):
            return True
        self.reports.write("NEEDS_REVIEW.txt", src, target, "NO_VERDICT", reason)
        return False

    # -- build -------------------------------------------------------------
    def compile_target(self, src: str, target: str, pre_pick: str) -> CompileFailure | None:
        """None means it built (or the build is switched off)."""
        if not self.cfg.do_compile:
            tag = "[dry-run]" if self.cfg.dry_run else "[no-compile]"
            log(f"  {tag} would build {target} with: sbt {' '.join(self.cfg.sbt_tasks)}")
            return None

        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        slug = ref_slug(f"{src}--{target}")
        log_path = self.cfg.log_dir / f"{self.stamp}-{slug}.log"

        env = os.environ.copy()
        java_var = "SBT_JAVA_HOME_" + re.sub(r"[^A-Za-z0-9]", "_", target)
        java_home = os.environ.get(java_var)
        if java_home:
            env["JAVA_HOME"] = java_home

        with log_path.open("w", encoding="utf-8") as handle:
            handle.write(f"### {datetime.now():%Y-%m-%d %H:%M:%S}: {src} onto {target} "
                         f"at {self.git.short('HEAD')}\n")
            if java_home:
                handle.write(f"### JAVA_HOME={java_home} (from {java_var})\n")
            handle.write(self.git.out("log", "--oneline", f"{pre_pick}..HEAD") + "\n\n")

        # Incremental first; a failure there may just be stale sbt state left by
        # the previous branch, so fall back to a clean build before believing it.
        attempts = [self.cfg.sbt_tasks]
        if self.cfg.clean_retry and "clean" not in self.cfg.sbt_tasks:
            attempts.append(["clean", *self.cfg.sbt_tasks])

        for number, tasks in enumerate(attempts, start=1):
            joined = " ".join(tasks)
            if number == 1:
                log(f"  {target}: sbt {joined}  (log: {log_path})")
            else:
                warn(f"  {target}: retrying from scratch -- sbt {joined}")
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n### attempt {number}: sbt {joined}\n")
                handle.flush()
                try:
                    completed = subprocess.run(
                        [str(self.sbt_launcher()), "-batch", *tasks],
                        cwd=self.git.root, env=env, timeout=self.cfg.sbt_timeout,
                        stdout=handle, stderr=subprocess.STDOUT,
                    )
                    returncode, timed_out = completed.returncode, False
                except subprocess.TimeoutExpired:
                    returncode, timed_out = -1, True
            if not timed_out and returncode == 0:
                if number == 1:
                    log(f"  {target}: sbt build is clean")
                else:
                    log(f"  {target}: sbt build is clean after the clean rebuild "
                        "(the first failure was stale state)")
                return None
            if timed_out:
                break   # a clean rebuild only takes longer; do not double the budget

        # Only the last attempt's errors: attempt 1 may have failed on stale state,
        # which is exactly what the clean rebuild was there to discount.
        text = log_path.read_text(encoding="utf-8", errors="replace")
        tail = text.rsplit("### attempt ", 1)[-1]
        errors = [line for line in tail.splitlines() if line.startswith("[error]")]
        if timed_out:
            warn(f"  {target}: sbt timed out after {self.cfg.sbt_timeout}s")
            first_error = f"timed out after {self.cfg.sbt_timeout}s"
        else:
            warn(f"  {target}: sbt failed (exit {returncode}) even after a clean rebuild"
                 if len(attempts) > 1 else f"  {target}: sbt failed (exit {returncode})")
            first_error = errors[0][:160] if errors else f"exit {returncode}, no [error] line"
        # Keep the errors on their own so they are readable without the whole log.
        log_path.with_suffix(".errors").write_text("\n".join(errors) + "\n", encoding="utf-8")
        for line in errors[:8]:
            warn(f"      {line}")

        # Keep the broken tip so it can be checked out and poked at later.
        kept_ref = f"refs/failed/merge_branches/{self.stamp}/{slug}"
        self.git.run("update-ref", kept_ref, "HEAD", check=True)
        warn(f"  {target}: kept the failing tree as {kept_ref} "
             f"(git checkout {kept_ref[len('refs/'):]})")
        return CompileFailure(log_path, kept_ref, first_error)

    def sbt_launcher(self) -> Path:
        """Each worktree has its own build/sbt and its own build state."""
        return self.cfg.sbt or (self.git.root / "build" / "sbt")

    def on_master_already(self, src: Source) -> list[str]:
        """This branch's commits as they exist on upstream master, if they do.

        Needed on a rerun: master landed last night, only branch-4.1 is left, and
        its "(cherry picked from commit ...)" line still has to name the master
        commit rather than a sha that only exists here.
        """
        found = []
        for commit in src.commits:
            mine = self.commit_patch_id(commit)
            candidates = []
            remembered = self.ledger_commits.get((src.name, "master"))
            if remembered:
                candidates.append(remembered)
            subject = self.git.subject(commit)
            if is_specific_subject(subject):
                candidates += self.git.lines(
                    "log", "--format=%H", "--fixed-strings", f"--grep={subject}",
                    f"{src.base}..{self.cfg.upstream}/master")
            for candidate in candidates:
                if not self.git.has_ref(f"{candidate}^{{commit}}"):
                    continue
                if mine and self.commit_patch_id(candidate) == mine:
                    found.append(candidate)
                    break
        return found if len(found) == len(src.commits) else []

    def find_landed(self, created: list[str], depth: int = 100) -> list[str]:
        """Our commits as they exist on HEAD now, matched by patch id.

        push_target may have rebased, which rewrites the shas -- and can drop one
        entirely when the same patch is already upstream.  Position is therefore
        no guide at all: after a drop, the top commit is a stranger's.
        """
        wanted = {}
        for commit in created:
            patch_id = self.commit_patch_id(commit)
            if patch_id:
                wanted[patch_id] = commit
        if not wanted:
            return []
        found = []
        for sha in self.git.lines("rev-list", f"-{depth}", "HEAD"):
            if self.commit_patch_id(sha) in wanted:
                found.append(sha)
                if len(found) == len(wanted):
                    break
        return list(reversed(found))

    def ci_stands_in_for_build(self, src: Source, target: str) -> bool:
        """The fork's CI already built this change against this very branch.

        Only when all three hold: CI was green, it ran on the change we are
        picking (not some older revision), and the branch was cut from this
        target -- so a branch off master covers master, one off branch-3.5
        covers branch-3.5, and neither covers anything else.
        """
        return (self.cfg.ci_shortcut and src.ci_green and src.ci_covers_change
                and src.base_branch == target)

    # -- push --------------------------------------------------------------
    def push_target(self, target: str) -> bool:
        with self.push_locks[target]:
            return self._push_target(target)

    def _push_target(self, target: str) -> bool:
        if not self.cfg.do_push:
            tag = "[dry-run]" if self.cfg.dry_run else "[no-push]"
            log(f"  {tag} would push {target} -> {self.cfg.push_remote}/{target} "
                "(pass --push to do it)")
            return True
        complaint = "no output"
        for attempt in (1, 2, 3):
            if self.git.run("push", self.cfg.push_remote,
                            f"HEAD:refs/heads/{target}").returncode == 0:
                log(f"  pushed {target} -> {self.cfg.push_remote}")
                return True
            # Keep it now: any later git call overwrites last_stderr.
            complaint = self.git.why()
            # Rebasing only helps a race, and git always names one of these when
            # that is what happened.  "[remote rejected]" on its own is a refusal
            # -- no write access, a server hook, a full disk -- and retrying just
            # buries the reason.
            if not any(word in self.git.last_stderr for word in
                       ("non-fast-forward", "fetch first", "stale info")):
                warn(f"  push of {target} failed: {complaint}")
                return False
            # Rebase onto the push remote itself, not its mirror: the mirror can
            # be minutes behind, and rebasing onto a stale ref just gets rejected
            # again.
            if self.git.ok("fetch", self.cfg.push_remote, target):
                onto, where = "FETCH_HEAD", f"{self.cfg.push_remote}/{target}"
            else:
                self.git.run("fetch", self.cfg.upstream, target)
                onto = where = f"{self.cfg.upstream}/{target}"
            warn(f"  push of {target} rejected (attempt {attempt}: {complaint}) -- "
                 f"rebasing on {where} and retrying")
            if not self.git.ok("rebase", onto):
                self.git.run("rebase", "--abort")
                break
        warn(f"  could not push {target} to {self.cfg.push_remote}: {complaint}")
        return False

    # -- one (source, target) pair ----------------------------------------
    def empty_pick(self) -> bool:
        """Did the cherry-pick stop because it would change nothing?

        The commit is already in the target, but under a sha `git cherry` could
        not match it by patch id -- it went in rewritten, squashed into
        something bigger, or with a fixup on top.  git stops the same way it
        stops for a conflict, so tell the two apart by the state it left: a
        conflict has unmerged paths, an empty pick has a clean tree and nothing
        staged.  Read the state rather than git's wording, which moves between
        versions.
        """
        if not self.git.has_ref("CHERRY_PICK_HEAD"):
            return False
        if self.git.out("ls-files", "--unmerged"):
            return False
        return not self.git.out("status", "--porcelain", "--untracked-files=no")

    def backport(self, src: Source, target: str,
                 from_commits: list[str] | None = None) -> list[str]:
        """Land the branch on `target`; returns the commits it created there.

        `from_commits` is the series as it landed on master.  Release branches
        are picked from those rather than from the local branch, so the
        "(cherry picked from commit ...)" line points at a commit that exists in
        apache/spark instead of a sha that only exists on this laptop.  With no
        such commit to point at, the trailer is left off entirely.
        """
        if not self.use_target(target):
            warn(f"cannot check out {target}: {self.git.why()}")
            self.reports.write("NEEDS_REVIEW.txt", src.name, target, "CHECKOUT_FAILED",
                               self.git.why())
            self.record(src.name, target, "could not check out the target")
            return []
        # Roll back to here, not to upstream: earlier branches in this run may
        # already have been pushed.
        pre_pick = self.git.out("rev-parse", "HEAD")

        # Commits already present in the target (same patch id) are skipped.
        already = {
            line.split()[1]
            for line in self.git.lines("cherry", "HEAD", src.head, src.base)
            if line.startswith("-")
        }

        # (the commit as it is on this branch, the one to actually pick)
        pairs = list(zip(src.commits, from_commits or src.commits))
        flags = ["-x"] if from_commits else []

        picked = 0
        for mine, commit in pairs:
            if mine in already:
                log(f"  {target}: {mine[:9]} already present, skipping")
                self.reports.write("already_landed.txt", src.name, target, commit,
                                   self.git.subject(commit),
                                   f"same patch is already on {target}")
                continue
            if self.git.ok("cherry-pick", *flags, commit):
                log(f"  {target}: picked {commit[:9]} {self.git.subject(commit)[:60]}")
                picked += 1
                continue
            if self.empty_pick():
                # Nothing to resolve and nothing to apply: the work is in there
                # already.  One commit per cherry-pick, so --abort undoes this
                # one and leaves anything picked earlier in the loop alone.
                log(f"  {target}: {commit[:9]} changes nothing here -- already landed")
                self.git.run("cherry-pick", "--abort")
                self.reports.write("already_landed.txt", src.name, target, commit,
                                   self.git.subject(commit),
                                   f"cherry-pick onto {target} came up empty")
                continue
            warn(f"  {target}: {commit[:9]} did not apply cleanly")
            self.git.run("cherry-pick", "--abort")
            self.reports.write("rejects.txt", src.name, target, commit,
                               self.git.subject(commit), src.pick_name,
                               f"does not apply to {target}")
            self.record(src.name, target, "conflict, nothing applied")
            self.git.run("reset", "--hard", pre_pick, check=True)
            warn(f"  {target}: rolled back to {pre_pick[:9]} after failed backport of {src.name}")
            return []

        if picked == 0:
            log(f"  {target}: nothing new to push")
            self.reports.write("skipped.txt", src.name, target,
                               "every commit was already there, see already_landed.txt")
            self.record(src.name, target, "already present, nothing to do")
            return []

        # Grab them now, while nothing has had a chance to rewrite them.
        created = self.git.lines("rev-list", "--reverse", f"-{picked}", "HEAD")

        if not self.sanity_check(src.name, target, f"{pre_pick}..HEAD"):
            self.git.run("reset", "--hard", pre_pick, check=True)
            warn(f"  {target}: not approved -- rolled back to {pre_pick[:9]}, see NEEDS_REVIEW.txt")
            self.record(src.name, target, "held for review")
            return []

        if self.ci_stands_in_for_build(src, target):
            log(f"  {target}: CI already passed on {self.cfg.fork_remote}/{src.fork_name}, "
                f"which is cut from {target} -- not rebuilding")
            failure = None
        else:
            failure = self.compile_target(src.name, target, pre_pick)
        if failure:
            self.reports.write("COMPILE_FAILED.txt", src.name, target, failure.log,
                               failure.kept_ref, failure.first_error)
            self.git.run("reset", "--hard", pre_pick, check=True)
            warn(f"  {target}: build failed -- rolled back to {pre_pick[:9]}, "
                 "see COMPILE_FAILED.txt")
            self.record(src.name, target, "build failed")
            return []

        if not self.push_target(target):
            # Leave nothing behind: the next source on this target would otherwise
            # push these commits along with its own, with no ledger row for them.
            self.git.run("reset", "--hard", pre_pick, check=True)
            warn(f"  {target}: push failed -- rolled back to {pre_pick[:9]}, nothing published")
            self.record(src.name, target, "push failed")
            return []
        self.record(src.name, target, "would push" if not self.cfg.do_push else "pushed")
        landed = self.find_landed(created)
        if not landed:
            warn(f"  {target}: cannot find the commit that landed -- the release "
                 "branches will be picked from the local branch instead")
        self.remember_merge(src, target, landed)
        return landed

    def remember_merge(self, src: Source, target: str, landed: list[str]) -> None:
        """Note a merge that really went out, so a rerun leaves it alone."""
        if not self.cfg.do_push:      # nothing left the machine, nothing to remember
            return
        if self.cfg.dry_run:
            log(f"  [dry-run] would record {src.name} -> {target} in {self.cfg.ledger}")
            return
        with self.ledger_lock:
            self._append_ledger(src, target, landed)

    def _append_ledger(self, src: Source, target: str, landed: list[str]) -> None:
        fresh = not self.cfg.ledger.exists() or self.cfg.ledger.stat().st_size == 0
        with self.cfg.ledger.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if fresh:
                writer.writerow(LEDGER_COLUMNS)
            writer.writerow([datetime.now().astimezone().isoformat(timespec="seconds"),
                             src.name, target, landed[-1] if landed else "",
                             src.head, self.cfg.push_remote])
        self.merged.add((src.name, target))

    def unlisted_sibling(self, src: Source, target: str) -> str | None:
        """A sibling cut for `target` that exists but was not put in the list."""
        stem = family_stem(src.name, self.cfg.strip_suffixes)
        for suffix, mapped in SUFFIX_TARGETS:
            if mapped != target:
                continue
            # Same spellings suffix_target() reads back, written out: a sibling
            # named foo-branch-4.x is no less a sibling than foo-4.x.
            for spelling in (suffix, f"-branch{suffix}"):
                for marker in ["", *self.cfg.strip_suffixes]:
                    candidate = f"{stem}{spelling}{marker}"
                    if candidate in self.listed or candidate == src.name:
                        return None
                    if (self.git.has_ref(f"refs/remotes/{self.cfg.fork_remote}/{candidate}")
                            or self.git.has_ref(f"refs/heads/{candidate}")):
                        return candidate
        return None

    # -- one source branch -------------------------------------------------
    def plan(self, src: Source) -> list[str] | None:
        """Everything that can be decided without touching a target branch.

        Returns the targets left to merge to, or None if there is nothing to do.
        """
        base = self.pick_base(src)
        if isinstance(base, str) and base.startswith("merged:"):
            merged_into = base.split(":", 1)[1]
            warn(f"{src.name}: already contained in {self.cfg.upstream}/{merged_into} -- skipping")
            warn(f"   (pin its base as a second column in {self.cfg.branch_file} if it still "
                 "needs backporting)")
            # The already-landed group, with the empty cherry-picks: same fact
            # about the same work, whether it turns up branch-at-a-time here or
            # commit-at-a-time in backport().
            self.reports.write("already_landed.txt", src.name, "-", src.head,
                               self.git.subject(src.fork_head or src.head),
                               f"already contained in {self.cfg.upstream}/{merged_into}")
            self.record(src.name, "-", f"skipped, already in {merged_into}")
            return None
        if not isinstance(base, tuple):
            warn(f"{src.name}: cannot work out what it branched from -- skipping")
            return None
        src.base, src.base_branch = base

        src.commits = self.git.lines("rev-list", "--reverse", "--no-merges",
                                     f"{src.base}..{src.head}")
        if not src.commits:
            warn(f"{src.name}: no commits over its base {src.base[:9]} -- skipping")
            return None
        if len(src.commits) > self.cfg.max_commits:
            warn(f"{src.name}: {len(src.commits)} commits over base {src.base[:9]} -- "
                 "that base looks wrong, skipping")
            warn(f"   (pin the base in {self.cfg.branch_file}, or raise --max-commits if it "
                 "really is that big)")
            self.reports.write("NEEDS_REVIEW.txt", src.name, "-", "TOO_MANY_COMMITS",
                               f"{len(src.commits)} commits over {src.base[:9]}")
            self.record(src.name, "-", "skipped, base looks wrong")
            return None
        merges = self.git.out("rev-list", "--count", "--merges", f"{src.base}..{src.head}")
        if merges not in ("", "0"):
            warn(f"{src.name}: ignoring {merges} merge commit(s) in the range")


        log(f"=== {src.name}: {len(src.commits)} commit(s) to backport "
            f"(cut from {src.base_branch} at {src.base[:9]})")

        # More than one commit: squash it first, do not spread the series around.
        if len(src.commits) > 1:
            log(f"{src.name}: {len(src.commits)} commits, not cherry-picking it "
                f"-> {SQUASH_FILE}")
            for commit in src.commits:
                log(f"      {commit[:9]} {self.git.subject(commit)[:70]}")
            self.reports.write(SQUASH_FILE, src.name, len(src.commits), src.head,
                               src.pick_name,
                               f"https://github.com/{self.cfg.fork_repo}/tree/{src.fork_name}")
            self.record(src.name, "-", "needs squashing first")
            return None

        readable = logical_name(src.name, self.cfg.strip_suffixes)
        if readable != src.name:
            log(f"  {src.name}: routed as {readable}")

        targets = targets_for(src.name, src.base_branch, self.valid_targets,
                              self.cfg.strip_suffixes)
        if targets != self.valid_targets:
            log(f"  {src.name} is pinned to: {' '.join(targets)}")
        if targets == ["branch-3.5"]:
            why = ("pinned by branch name"
                   if suffix_target(src.name, self.cfg.strip_suffixes) == "branch-3.5"
                   else "cut from branch-3.5")
            log(f"  {src.name}: branch-3.5 only -> 3.5only.txt")
            self.reports.write("3.5only.txt", src.name, src.head, why,
                               f"https://github.com/{self.cfg.fork_repo}/tree/{src.fork_name}")

        covered = sibling_coverage(src.name, targets, self.listed, self.cfg.strip_suffixes)
        for target, sibling in covered.items():
            log(f"  {target}: left to {sibling}, which is the cut for it")
            self.reports.write("skipped.txt", src.name, target,
                               f"covered by the sibling branch {sibling}")
            self.record(src.name, target, "left to a sibling branch")
        targets = [target for target in targets if target not in covered]
        if not targets:
            log(f"  {src.name}: every target belongs to a sibling branch")
            return None
        for target in targets:
            unlisted = self.unlisted_sibling(src, target)
            if unlisted:
                warn(f"  {target}: there is a branch {unlisted} you have not listed -- "
                     f"add it if that is the cut {target} needs")

        # Before the diff prompt: nothing to look at if it is all done already.
        done = [target for target in targets if (src.name, target) in self.merged]
        remaining = [target for target in targets if target not in done]
        if done and not remaining:
            log(f"  {src.name}: skipping, already merged to every target it goes to "
                f"({', '.join(done)}) -- see {self.cfg.ledger}")
            for target in done:
                self.record(src.name, target, "already merged in an earlier run")
            return None
        for target in done:
            log(f"  {target}: already merged in a previous run ({self.cfg.ledger})")
            self.record(src.name, target, "already merged in an earlier run")
        targets = remaining

        landed = {target: self.already_landed(target, src) for target in targets}
        for target, verdict in landed.items():
            if not verdict:
                continue
            outcome, reason = verdict
            if outcome == "skip":
                log(f"  {target}: {reason} -> skipped.txt")
                self.reports.write("skipped.txt", src.name, target, reason)
                self.record(src.name, target, "already landed upstream")
            else:   # worth a look, but not a reason to drop it
                warn(f"  {target}: {reason}")
                self.reports.write("NEEDS_REVIEW.txt", src.name, target,
                                   "SAME_SUBJECT_DIFFERENT_PATCH", reason)
        targets = [target for target in targets
                   if not (landed[target] and landed[target][0] == "skip")]
        if not targets:
            log(f"  {src.name}: nothing left to do, it is already everywhere it goes")
            return None

        return targets

    def execute(self, src: Source, targets: list[str]) -> None:
        """Do the merging.  Runs in this Backporter's worktree."""
        # master first: what lands there is what the release branches get picked
        # from, so their cherry-pick trailers name a commit that exists upstream.
        # If master is not in this run it may still have landed earlier; the
        # release branches are picked from that commit either way.
        on_master: list[str] = [] if "master" in targets else self.on_master_already(src)
        if on_master:
            log(f"  {src.name}: already on master as {on_master[-1][:9]} -- the backports "
                "will be picked from there")
        for target in sorted(targets, key=lambda name: name != "master"):
            if target == src.name:
                continue
            if not self.ensure_target(target):
                self.reports.write("NEEDS_REVIEW.txt", src.name, target, "NO_SUCH_TARGET",
                                   f"pinned target missing on {self.cfg.upstream}")
                self.record(src.name, target, "pinned target does not exist")
                continue
            landed_as = self.backport(src, target, from_commits=on_master or None)
            if target == "master":
                on_master = landed_as

    # -- the run -----------------------------------------------------------
    def run(self, sources: list[Source]) -> None:
        log(f"fetching {self.cfg.fork_remote} and {self.cfg.upstream} ...")
        if not self.git.ok("fetch", self.cfg.fork_remote):
            raise Bail(f"fetch {self.cfg.fork_remote} failed")
        if not self.git.ok("fetch", self.cfg.upstream):
            raise Bail(f"fetch {self.cfg.upstream} failed")

        self.listed = [src.name for src in sources]
        if self.held:
            log(f"{len(self.held)} branch(es) are held in {self.cfg.hold_list}")
        green: list[Source] = []
        waiting: list[Source] = []       # CI still running; revisited at the end
        failed: list[Source] = []        # CI red once; looked at once more at the end
        for src in sources:
            if src.name in self.held:
                log(f"{src.name}: held in {self.cfg.hold_list} ({self.held[src.name]}) "
                    "-- skipping it")
                self.record(src.name, "-", "held, listed in " + str(self.cfg.hold_list))
                continue
            found = self.resolve_on_fork(src.name)
            if not found:
                tried = " or ".join(dict.fromkeys(
                    [src.name, logical_name(src.name, self.cfg.strip_suffixes)]))
                warn(f"{src.name}: not found on {self.cfg.fork_remote} "
                     f"(tried {tried}) -- skipping")
                self.reports.write("TO_MERGE_LATER.txt", src.name, "",
                                   f"NOT_ON_FORK: tried {tried}")
                self.record(src.name, "-", "skipped, not on the fork")
                continue
            src.fork_name, src.fork_head = found
            if src.fork_name != src.name:
                log(f"{src.name}: CI checked on {self.cfg.fork_remote}/{src.fork_name}")
            src.pick_name, src.head = self.resolve_pick_ref(src)

            if self.cfg.skip_ci:
                log(f"{src.name}: CI check skipped")
                green.append(src)
                continue

            url = f"https://github.com/{self.cfg.fork_repo}/tree/{src.fork_name}"
            # CI ran on the fork's commit, never on the local -aok rewrite.
            state = self.ci_status_of(src.fork_head)
            if state == "passing":
                log(f"{src.name}: CI green ({src.fork_head[:9]})")
                src.ci_green = True
                green.append(src)
            elif state.startswith("running"):
                note = ("still running" if state == "running"
                        else "still running, but some jobs have already failed")
                log(f"{src.name}: CI {note} -- will come back to it")
                waiting.append(src)
            elif state == "failing":
                log(f"{src.name}: CI failing -- one more look at the end")
                failed.append(src)
            elif state == "error":
                warn(f"{src.name}: could not read CI status from GitHub -> TO_MERGE_LATER.txt")
                self.reports.write("TO_MERGE_LATER.txt", src.name, src.fork_head, "CI_API_ERROR")
                self.record(src.name, "-", "skipped, could not read CI")
            else:
                log(f"{src.name}: no CI results -> TO_MERGE_LATER.txt")
                self.reports.write("TO_MERGE_LATER.txt", src.name, src.fork_head, "NO_CI_RESULTS")
                self.record(src.name, "-", "skipped, no CI results")

        if not green and not waiting and not failed:
            log("no branches to merge")
            return
        if green:
            log("green: " + " ".join(src.name for src in green))
        if waiting:
            log("still building: " + " ".join(src.name for src in waiting))
        if failed:
            log("CI red, one retry each: " + " ".join(src.name for src in failed))
        for src in green:
            self.warn_if_untested(src)

        for target in dict.fromkeys(self.cfg.targets):   # de-dup, keep order
            if self.ensure_target(target):
                self.valid_targets.append(target)
        if not self.valid_targets:
            raise Bail(f"none of the target branches exist on {self.cfg.upstream}")

        if self.cfg.sanity:
            self.claude_ping()

        # 1. Audit everything first, so the questions come before the long work.
        #    In-flight branches are audited and approved now too, then merged
        #    later if their CI comes good -- one sitting of questions, not two.
        plans: list[tuple[Source, list[str]]] = []
        flight = {src.name for src in waiting}
        retry = {src.name for src in failed}
        for src in green + waiting + failed:
            targets = self.plan(src)
            if targets:
                plans.append((src, targets))
        if not plans:
            log("nothing left to merge")
            self.summary()
            return

        # 2. Ask about all of them up front, then nothing needs you again.
        families: dict[str, list[tuple[Source, list[str]]]] = defaultdict(list)
        for src, targets in plans:
            families[family_stem(src.name, self.cfg.strip_suffixes)].append((src, targets))

        approved = []
        for stem, members in families.items():
            if all(self.already_approved(src) for src, _ in members):
                names = ", ".join(src.name for src, _ in members)
                log(f"{names}: this diff was approved before -- not showing it again")
                approved.extend(members)
            elif self.operator_review(members):
                approved.extend(members)
        if not approved:
            log("nothing approved")
            self.summary()
            return
        if self.cfg.operator_review and not self.cfg.assume_yes:
            log(f"{len(approved)} branch(es) approved -- the rest of the run needs "
                "nothing from you")

        # 3. The slow part -- the ones whose CI is already green.
        held = flight | retry
        ready = [(src, targets) for src, targets in approved if src.name not in held]
        pending = [(src, targets) for src, targets in approved if src.name in flight]
        retrying = [(src, targets) for src, targets in approved if src.name in retry]
        self.merge_all(ready)

        # 4. Then keep coming back to the ones that were still building.
        if pending:
            retrying += self.revisit(pending)

        # 5. Last of all, one more look at everything CI called red.  A branch
        #    only gets this one extra shot: what is still red now is red.
        self.retry_failed(retrying)

        self.summary()

    def merge_all(self, work: list[tuple[Source, list[str]]]) -> None:
        if not work:
            return
        if self.cfg.jobs > 1 and len(work) > 1:
            self.run_in_parallel(work)
        else:
            for src, targets in work:
                self.execute(src, targets)

    def revisit(self, pending: list[tuple[Source, list[str]]]) -> list[tuple[Source, list[str]]]:
        """Come back to the branches that were still building, until none are.

        They were approved in the same sitting as everything else, so this needs
        nobody: check CI, merge whatever has gone green, sleep, repeat.

        Returns the ones whose build ended red, for retry_failed() to look at
        once more -- a build that finishes red here has failed exactly once,
        the same as one that was already red when the run started.
        """
        if self.cfg.dry_run:
            log(f"  [dry-run] would keep checking CI for {len(pending)} branch(es) "
                f"every {self.cfg.ci_poll_minutes} min and merge them as they go green")
            for src, _ in pending:
                self.record(src.name, "-", "would wait for CI")
            return []
        deadline = (time.time() + self.cfg.ci_wait_hours * 3600
                    if self.cfg.ci_wait_hours else None)
        late_failures: list[tuple[Source, list[str]]] = []
        while pending:
            log(f"checking CI for {len(pending)} branch(es) that were still building")
            self.git.run("fetch", self.cfg.fork_remote)
            ready, still = [], []
            for src, targets in pending:
                found = self.resolve_on_fork(src.name)
                if not found:
                    warn(f"{src.name}: gone from {self.cfg.fork_remote} -- giving up on it")
                    self.reports.write("TO_MERGE_LATER.txt", src.name, "",
                                       "disappeared from the fork while we waited")
                    self.record(src.name, "-", "vanished while waiting")
                    continue
                fork_name, fork_head = found
                if fork_head != src.fork_head:
                    warn(f"{src.name}: pushed again while we waited ({src.fork_head[:9]} "
                         f"-> {fork_head[:9]}) -- leaving it for the next run")
                    self.reports.write("TO_MERGE_LATER.txt", src.name, fork_head,
                                       "moved while we waited; re-run to pick it up")
                    self.record(src.name, "-", "moved while waiting")
                    continue
                state = self.ci_status_of(fork_head)
                url = f"https://github.com/{self.cfg.fork_repo}/tree/{fork_name}"
                if state == "passing":
                    log(f"{src.name}: CI went green -- merging it now")
                    src.ci_green = True
                    self.warn_if_untested(src)
                    ready.append((src, targets))
                elif state.startswith("running"):
                    still.append((src, targets))
                elif state == "failing":
                    log(f"{src.name}: CI failed in the end -- one more look later")
                    late_failures.append((src, targets))
                else:
                    log(f"{src.name}: no usable CI result -> TO_MERGE_LATER.txt")
                    self.reports.write("TO_MERGE_LATER.txt", src.name, fork_head, state)
                    self.record(src.name, "-", f"skipped, CI {state}")

            self.merge_all(ready)
            pending = still
            if not pending:
                log("nothing left building")
                return late_failures
            if deadline and time.time() >= deadline:
                warn(f"gave up waiting on {len(pending)} branch(es) after "
                     f"{self.cfg.ci_wait_hours}h")
                for src, _ in pending:
                    self.reports.write("TO_MERGE_LATER.txt", src.name, src.fork_head,
                                       "still building when the run ended")
                    self.record(src.name, "-", "still building at the end")
                return late_failures
            if not ready:
                # Nothing went green this round: everything left is queued
                # behind somebody else's CI.  Rather than sleep through the
                # interval, take the one at the head of the queue and let the
                # build this script runs anyway stand in for the CI that has not
                # started.  One per round -- the next round asks GitHub again
                # first and only comes back here if everything is still queued,
                # so this works its way down the list instead of sitting on one
                # branch.
                nudged = self.build_instead_of_waiting(pending)
                if nudged:
                    pending = [pair for pair in pending if pair[0].name != nudged]
                    continue                     # straight back to the CI check
            log(f"{len(pending)} branch(es) still building -- looking again in "
                f"{self.cfg.ci_poll_minutes} min")
            time.sleep(self.cfg.ci_poll_minutes * 60)
        return late_failures

    def build_instead_of_waiting(self, pending: list[tuple[Source, list[str]]]) -> str | None:
        """Merge the next queued branch on a local build instead of a green tick.

        The operator approved all of these in the same sitting, and a build here
        is the same build CI would run, so a clean one is reason enough to get
        on with it.  Returns the branch name that had its turn -- merged, or
        found wanting by the build and recorded as such -- so the caller can
        take it off the queue.  Nothing happens without a local build to lean
        on: with --no-compile there is no second opinion to have.
        """
        if not (self.cfg.local_stand_in and self.cfg.do_compile and pending):
            return None
        src, targets = pending[0]
        log(f"{src.name}: CI has not started on {self.cfg.fork_remote}/{src.fork_name} "
            "-- building it here, and merging it if the build is clean")
        # True either way, and honest either way: the per-target rows that
        # execute() writes say whether it went in or the build stopped it.
        self.record(src.name, "-", "built here while its CI was still queued")
        self.execute(src, targets)
        return src.name

    # -- re-cutting the local rewrites ------------------------------------
    def run_helper(self, script: Path, args: list[str], env: dict[str, str]) -> bool:
        """Run one of the repo's shell helpers, its output kept in the log dir.

        A helper that is missing, times out or comes back non-zero is not fatal:
        say so and carry on with the rewrites as they already are.
        """
        path = script if script.is_absolute() else self.git.root / script
        if not os.access(path, os.X_OK):
            warn(f"{script}: not there or not executable -- leaving the rewrites alone")
            return False
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.cfg.log_dir / f"{self.stamp}-{path.stem}.log"
        log(f"  running {path.name} {' '.join(args)}".rstrip() + f" -> {log_path}")
        try:
            # squash-magic.sh asks the operator for a commit message on /dev/tty
            # when the claude CLI is not answering, the same way this script asks
            # about a diff, so it keeps the terminal; the timeout is the backstop.
            result = subprocess.run(
                [str(path), *args], cwd=self.git.root, text=True,
                env={**os.environ, **env}, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=self.cfg.refresh_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            warn(f"{path.name}: gave up after {self.cfg.refresh_timeout}s")
            log_path.write_text(exc.stdout or "", encoding="utf-8")
            return False
        except OSError as exc:
            warn(f"{path.name}: {exc}")
            return False
        log_path.write_text(result.stdout or "", encoding="utf-8")
        if result.returncode != 0:
            warn(f"{path.name} exited {result.returncode} -- see {log_path}")
            return False
        return True

    def refresh_rewrites(self,
                         work: list[tuple[Source, list[str]]]
                         ) -> list[tuple[Source, list[str]]]:
        """Cut the local -aok/-squashed branches again before the last look.

        The rewrite named in the branch file was made at some point in the past,
        rebased onto the base branches as they stood then.  By the time a branch
        whose CI went red gets its one extra look, the bases have moved and the
        branch may have been rebased or had a fix pushed to it, so the local ref
        we would cherry-pick from is a stale cut of the work.  update-bases.sh
        fast-forwards the bases, then squash-magic.sh cuts the rewrites again
        from the fork's current tips -- only for the branches being retried.

        squash-magic.sh truncates its own bookkeeping files, so they are pointed
        at the log directory: emptying branches_to_merge.txt, the file this run
        was read from, while the run is still going would be unkind.

        A branch whose rewrite really did move is planned again -- a new head has
        its own base, its own commits, and possibly its own set of targets.  The
        name from the branch file stays the name in every report and in the
        ledger, even when the rewrite comes back under a different marker.
        """
        if not work or not self.cfg.refresh_before_retry:
            return work
        if self.cfg.pick_from_fork:
            log("--pick-from-fork: the local rewrites are not picked from, "
                "so they are left alone")
            return work
        names = list(dict.fromkeys(logical_name(src.name, self.cfg.strip_suffixes)
                                   for src, _ in work))
        if self.cfg.dry_run:
            log(f"  [dry-run] would run {self.cfg.update_bases} and then "
                f"{self.cfg.squash_magic} for: {' '.join(names)}")
            return work

        log(f"re-cutting the rewrites for {len(names)} branch(es) before the second "
            "look: " + " ".join(names))
        # The bases this script resets its targets to are the ones the rewrites
        # should sit on, so update-bases.sh is pointed at the same upstream.
        if not self.run_helper(self.cfg.update_bases, [], {"REMOTE": self.cfg.upstream}):
            return work
        out = (self.cfg.log_dir / f"{self.stamp}-refresh-branches.txt").absolute()
        scratch = {name: str((self.cfg.log_dir / f"{self.stamp}-refresh-{part}.txt").absolute())
                   for name, part in [("UNKNOWN_FILE", "unknown"), ("WTFBBQ_FILE", "wtfbbq"),
                                      ("IDK_FILE", "idk")]}
        if not self.run_helper(self.cfg.squash_magic, names,
                               {"REMOTE": self.cfg.fork_remote, "OUTPUT_FILE": str(out),
                                **scratch}):
            return work

        try:
            made = squash_output(out.read_text(encoding="utf-8"), self.cfg.strip_suffixes)
        except OSError as exc:
            warn(f"cannot read {out}: {exc} -- leaving the rewrites alone")
            return work

        refreshed: list[tuple[Source, list[str]]] = []
        for src, targets in work:
            cut = made.get(logical_name(src.name, self.cfg.strip_suffixes))
            if cut is None:
                # It went to squash-magic's unknown/wtfbbq pile instead: no clean
                # base, or somebody else's commits on it.  Its own log says which.
                warn(f"{src.name}: squash-magic.sh cut no rewrite for it -- keeping "
                     f"{src.pick_name} ({src.head[:9]})")
                refreshed.append((src, targets))
                continue
            head = self.git.rev_parse(f"refs/heads/{cut}^{{commit}}")
            if not head:
                warn(f"{src.name}: no local {cut} after re-cutting -- keeping "
                     f"{src.pick_name} ({src.head[:9]})")
                refreshed.append((src, targets))
                continue
            if (cut, head) == (src.pick_name, src.head):
                log(f"{src.name}: {cut} is unchanged ({head[:9]})")
                refreshed.append((src, targets))
                continue
            log(f"{src.name}: picking {cut} ({src.head[:9]} -> {head[:9]})")
            src.pick_name, src.head = cut, head
            again = self.plan(src)
            if not again:
                log(f"{src.name}: nothing left to merge once it was re-cut")
                continue
            refreshed.append((src, again))
        return refreshed

    def retry_failed(self, work: list[tuple[Source, list[str]]]) -> None:
        """The second and last look at the branches whose CI came back red.

        A red run is often a flake, an infrastructure hiccup, or a job somebody
        re-ran while this script was busy merging everything else, so ask GitHub
        once more now that the rest of the run is done.  Once more is the whole
        budget: what is still red here goes to FAILING_CI.txt and stays there
        until the next run.

        The local rewrites are cut again first (see refresh_rewrites): whatever
        is merged here is merged at the end of a long run, onto bases that have
        moved since the -aok and -squashed branches were made.
        """
        if not work:
            return
        work = self.refresh_rewrites(work)      # --dry-run is handled in there
        if not work:
            log("nothing left to look at once the rewrites were re-cut")
            return
        if self.cfg.dry_run:
            log(f"  [dry-run] would take one more look at CI for {len(work)} "
                "branch(es) that failed")
            for src, _ in work:
                self.record(src.name, "-", "would re-check failing CI")
            return

        log(f"one more look at CI for {len(work)} branch(es) that failed: "
            + " ".join(src.name for src, _ in work))
        self.git.run("fetch", self.cfg.fork_remote)
        ready: list[tuple[Source, list[str]]] = []
        for src, targets in work:
            found = self.resolve_on_fork(src.name)
            if not found:
                warn(f"{src.name}: gone from {self.cfg.fork_remote} -- giving up on it")
                self.reports.write("TO_MERGE_LATER.txt", src.name, "",
                                   "disappeared from the fork after its CI failed")
                self.record(src.name, "-", "vanished after failing CI")
                continue
            fork_name, fork_head = found
            url = f"https://github.com/{self.cfg.fork_repo}/tree/{fork_name}"
            if fork_head != src.fork_head:
                # The approval was for the diff that failed; a new push is a new
                # diff and wants looking at again from the top.
                warn(f"{src.name}: pushed again after its CI failed "
                     f"({src.fork_head[:9]} -> {fork_head[:9]}) -- leaving it for "
                     "the next run")
                self.reports.write("TO_MERGE_LATER.txt", src.name, fork_head,
                                   "moved after its CI failed; re-run to pick it up")
                self.record(src.name, "-", "moved after failing CI")
                continue
            state = self.ci_status_of(fork_head)
            if state == "passing":
                log(f"{src.name}: CI is green on the second look -- merging it")
                src.ci_green = True
                self.warn_if_untested(src)
                ready.append((src, targets))
            elif state == "failing":
                log(f"{src.name}: CI still failing -> FAILING_CI.txt")
                self.reports.write("FAILING_CI.txt", src.name, fork_head, url)
                self.record(src.name, "-", "skipped, CI failing twice")
            else:
                # Re-running the red jobs puts the commit back to "running", and
                # so does a fresh workflow.  That is not a second failure, but
                # the extra shot is spent either way -- the next run can have it.
                log(f"{src.name}: CI {state} on the second look -> TO_MERGE_LATER.txt")
                self.reports.write("TO_MERGE_LATER.txt", src.name, fork_head,
                                   f"CI {state} when it was re-checked after failing")
                self.record(src.name, "-", f"skipped, CI {state} on the re-check")

        self.merge_all(ready)

    def run_in_parallel(self, approved: list[tuple[Source, list[str]]]) -> None:
        """One worktree per worker; a worker owns a whole branch at a time."""
        count = min(self.cfg.jobs, len(approved))
        worktrees = self.make_worktrees(count)
        if not worktrees:
            warn("no worktrees available -- doing it one at a time")
            for src, targets in approved:
                self.execute(src, targets)
            return

        free: queue.Queue = queue.Queue()
        for number, path in enumerate(worktrees, start=1):
            free.put((f"[w{number}] ", self.for_worktree(path, f"[w{number}] ")))

        done = itertools.count(1)
        total = len(approved)

        def one(src: Source, targets: list[str]) -> None:
            tag, worker = free.get()
            worker_tag(tag)
            try:
                worker.execute(src, targets)
                worker_tag("")
                log(f"progress: {next(done)}/{total} branches finished")
            except Exception as exc:                      # keep one branch's blow-up
                warn(f"{src.name}: worker failed: {exc}")  # from taking down the run
                self.record(src.name, "-", "worker error")
            finally:
                # However it ended, the next branch needs a clean tree: an
                # interrupted cherry-pick would block the checkout and the branch
                # would vanish from the run without a word.
                worker.git.run("cherry-pick", "--abort")
                worker.git.run("rebase", "--abort")
                worker.git.run("reset", "--hard")
                worker_tag("")
                free.put((tag, worker))

        log(f"merging {len(approved)} branch(es) across {len(worktrees)} worktree(s)")
        with ThreadPoolExecutor(max_workers=len(worktrees)) as pool:
            futures = [pool.submit(one, src, targets) for src, targets in approved]
            for future in as_completed(futures):
                future.result()

    def make_worktrees(self, count: int) -> list[Path]:
        """Reused between runs: they hold each worker's sbt build state."""
        base = self.git.root / WORKTREE_DIR
        # A worktree directory deleted by hand stays registered and blocks the
        # name; clear those out before asking for anything.
        self.git.run("worktree", "prune")
        paths = []
        for number in range(1, count + 1):
            path = base / f"w{number}"
            if not (path / ".git").exists():
                log(f"creating worktree {path} (a full checkout, takes a moment)")
                if not self.git.ok("worktree", "add", "--detach", "--force", str(path),
                                   f"{self.cfg.upstream}/master"):
                    warn(f"could not create {path}: {self.git.why()}")
                    break
            paths.append(path)
        return paths


# ------------------------------------------------------------------------ main
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-f", "--file", default="branches_to_merge.txt", type=Path,
                        help="branch list (default: %(default)s)")
    parser.add_argument("-t", "--targets", default=" ".join(DEFAULT_TARGETS),
                        help="space separated target branches (default: %(default)s)")
    parser.add_argument("--fork", default="holdenk/spark", help="fork checked for CI")
    parser.add_argument("--fork-remote", default="origin", help="git remote for the fork")
    parser.add_argument("--upstream", default="apache-github", help="remote to rebase on")
    parser.add_argument("--push-remote", default="apache", help="remote to push to")
    parser.add_argument("--push", action="store_true", help="actually push (default: dry run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="only say what would happen: no push, no sbt build, no report "
                             "files written, no prompts. Local git state IS still changed: "
                             "the target branches are reset to upstream (anything ahead is "
                             "saved under refs/backup/merge_branches/) and the cherry-picks "
                             "are tried on them, so the conflicts reported are real")
    parser.add_argument("--skip-ci", action="store_true", help="treat every branch as green")
    parser.add_argument("--no-sanity-check", action="store_true",
                        help="skip the claude/human review gate")
    parser.add_argument("--review-model", default="sonnet", help="model for the review")
    parser.add_argument("--review-timeout", type=int, default=300)
    parser.add_argument("--max-diff-bytes", type=int, default=100_000)
    parser.add_argument("-y", "--assume-yes", action="store_true",
                        help="never prompt: push what claude calls OK, everything else goes "
                             "to NEEDS_REVIEW.txt")
    parser.add_argument("--no-ci-shortcut", action="store_true",
                        help="build every target, even one the fork's CI already covered")
    parser.add_argument("--no-operator-review", action="store_true",
                        help="do not show each branch's diff and ask before merging it")
    parser.add_argument("--review-diff-lines", type=int, default=400,
                        help="how much of the diff to show before asking (default: %(default)s)")
    parser.add_argument("--pick-from-fork", action="store_true",
                        help="cherry-pick from the fork's copy instead of the local "
                             "branch under the listed name")
    parser.add_argument("--strip-suffix", action="append", metavar="SUFFIX",
                        help="ignore this suffix when matching a branch name to a target "
                             "(default: -aok and -squashed; repeatable)")
    parser.add_argument("--max-commits", type=int, default=30,
                        help="refuse a branch with a bigger range (guards a bad base guess)")
    parser.add_argument("--no-compile", action="store_true", help="skip the sbt build")
    parser.add_argument("--sbt", type=Path, help="sbt launcher (default: <repo>/build/sbt)")
    parser.add_argument("--sbt-tasks", default="compile Test/compile",
                        help="sbt tasks to run (default: %(default)s)")
    parser.add_argument("--no-clean-retry", action="store_true",
                        help="do not retry a failed build with a leading clean")
    parser.add_argument("--sbt-timeout", type=int, default=7200)
    parser.add_argument("--log-dir", type=Path, default=Path("merge_logs"))
    parser.add_argument("--ledger", type=Path, default=Path(LEDGER_FILE),
                        help="csv of merges already pushed, skipped on a rerun "
                             "(default: %(default)s)")
    parser.add_argument("-j", "--jobs", type=int, default=1, metavar="N",
                        help="merge N branches at once, each in its own worktree under "
                             f"{WORKTREE_DIR}/ (default: %(default)s). Every worker runs "
                             "its own sbt, so keep an eye on RAM")
    parser.add_argument("--ci-poll", type=float, default=10, metavar="MINUTES",
                        help="how often to re-check a branch whose CI was still running "
                             "(default: %(default)s)")
    parser.add_argument("--ci-wait", type=float, default=12.0, metavar="HOURS",
                        help="how long to keep coming back to them, 0 for no limit "
                             "(default: %(default)s)")
    parser.add_argument("--approvals", type=Path, default=Path(APPROVALS_FILE),
                        help="diffs you have already approved, not shown again "
                             "(default: %(default)s)")
    parser.add_argument("--hold-list", type=Path, default=Path(HOLD_FILE),
                        help="branches named in this file are skipped outright "
                             "(default: %(default)s)")
    parser.add_argument("--ignore-hold-list", action="store_true",
                        help="do not skip the branches named in the hold list")
    parser.add_argument("--ignore-ledger", action="store_true",
                        help="do the merges again even if the ledger says they are done")
    parser.add_argument("--no-local-build-stand-in", action="store_true",
                        help="while waiting on CI, do not merge a queued branch on the "
                             "strength of a local build; just keep polling")
    parser.add_argument("--no-refresh-before-retry", action="store_true",
                        help="do not re-run update-bases.sh and squash-magic.sh before the "
                             "last look at the branches whose CI was red")
    parser.add_argument("--update-bases-script", type=Path, default=UPDATE_BASES_SCRIPT,
                        metavar="PATH", help="fast-forwards the base branches "
                                             "(default: %(default)s)")
    parser.add_argument("--squash-script", type=Path, default=SQUASH_SCRIPT, metavar="PATH",
                        help="cuts the -aok/-squashed rewrites (default: %(default)s)")
    parser.add_argument("--refresh-timeout", type=int, default=1800, metavar="SECONDS",
                        help="how long either of those two may take (default: %(default)s)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.dry_run and args.push:
        warn("--dry-run wins over --push: nothing will be pushed")
    if args.dry_run and args.jobs > 1:
        warn("--dry-run runs on its own: a preview does not need the worktrees")
    do_push = args.push and not args.dry_run
    do_compile = not (args.no_compile or args.dry_run)

    if not shutil.which("gh"):
        raise Bail("the gh CLI is required")
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if root.returncode != 0:
        raise Bail("not inside a git repository")
    git = Git(Path(root.stdout.strip()))

    for remote in (args.upstream, args.fork_remote, *([args.push_remote] if do_push else [])):
        if not git.ok("remote", "get-url", remote):
            raise Bail(f"no remote '{remote}'")
    if git.out("status", "--porcelain", "--untracked-files=no"):
        raise Bail("working tree is dirty -- commit or stash first")

    sbt = args.sbt                       # None -> each worktree uses its own build/sbt
    if do_compile and not os.access(sbt or git.root / "build" / "sbt", os.X_OK):
        raise Bail("build/sbt is not executable (use --no-compile to skip the build)")

    cfg = Config(
        branch_file=args.file, targets=args.targets.split(), fork_repo=args.fork,
        fork_remote=args.fork_remote, upstream=args.upstream, push_remote=args.push_remote,
        do_push=do_push, skip_ci=args.skip_ci, sanity=not args.no_sanity_check,
        review_model=args.review_model, review_timeout=args.review_timeout,
        max_diff_bytes=args.max_diff_bytes, assume_yes=args.assume_yes,
        max_commits=args.max_commits, strip_suffixes=args.strip_suffix or STRIP_SUFFIXES,
        pick_from_fork=args.pick_from_fork,
        operator_review=not args.no_operator_review, review_diff_lines=args.review_diff_lines,
        ci_shortcut=not args.no_ci_shortcut,
        do_compile=do_compile, dry_run=args.dry_run, sbt=sbt,
        sbt_tasks=args.sbt_tasks.split(), clean_retry=not args.no_clean_retry,
        sbt_timeout=args.sbt_timeout, log_dir=args.log_dir,
        ledger=args.ledger, approvals=args.approvals, ignore_ledger=args.ignore_ledger,
        hold_list=args.hold_list, ignore_hold_list=args.ignore_hold_list,
        jobs=1 if args.dry_run else max(1, args.jobs),
        ci_poll_minutes=max(0.05, args.ci_poll), ci_wait_hours=args.ci_wait,
        refresh_before_retry=not args.no_refresh_before_retry,
        local_stand_in=not args.no_local_build_stand_in,
        update_bases=args.update_bases_script, squash_magic=args.squash_script,
        refresh_timeout=args.refresh_timeout,
    )

    try:   # a plain path, but also process substitution or /dev/stdin
        branch_text = args.file.read_text(encoding="utf-8")
    except OSError as exc:
        raise Bail(f"cannot read {args.file}: {exc}") from exc
    sources = [Source(name, base) for name, base in parse_branch_file(branch_text)]
    if not sources:
        raise Bail(f"{args.file} contains no branches")
    log(f"read {len(sources)} branch(es) from {args.file}")
    if cfg.jobs > 1 and cfg.do_compile:
        try:    # each sbt wants several GB; better a warning now than the OOM killer
            free_gb = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30
            if cfg.jobs * 6 > free_gb:
                warn(f"{cfg.jobs} parallel sbt builds want roughly {cfg.jobs * 6} GB "
                     f"but only {free_gb:.0f} GB looks free -- consider fewer jobs")
        except (ValueError, OSError):
            pass

    now = datetime.now().astimezone()   # astimezone so %z is populated
    reports = Reports(now.strftime("%Y-%m-%d %H:%M:%S %z"), Path.cwd(), dry_run=args.dry_run)
    started_on = git.out("symbolic-ref", "--quiet", "--short", "HEAD") or git.out("rev-parse", "HEAD")

    backporter = Backporter(cfg, git, reports, now.strftime("%Y%m%d-%H%M%S"))
    try:
        backporter.run(sources)
    finally:
        git.run("cherry-pick", "--abort")
        git.run("checkout", started_on)

    if args.dry_run:
        log("done (dry run). Nothing was pushed, built or written; re-run without "
            "--dry-run to do it for real.")
    else:
        log("done. see TO_MERGE_LATER.txt, FAILING_CI.txt, rejects.txt, NEEDS_REVIEW.txt,")
        log(f"          skipped.txt, already_landed.txt, {SQUASH_FILE},")
        log("          operator-rejected.txt,")
        log(f"          COMPILE_FAILED.txt (build logs in {cfg.log_dir}/) and 3.5only.txt")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Bail as exc:
        warn(str(exc))
        sys.exit(1)
    except KeyboardInterrupt:
        warn("interrupted")
        sys.exit(130)
