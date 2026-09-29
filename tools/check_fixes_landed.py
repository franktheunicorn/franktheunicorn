#!/usr/bin/env python3
"""Double-check that the listed fixes have actually landed upstream.

merge_branches.py says what it did; this says what is true.  It reads the same
branch file and, for every branch, works out the targets the same routing rules
send it to, then looks for the change itself on each upstream target branch --
by patch id, by the "(cherry picked from commit ...)" trailer, or by a matching
subject with a matching patch: the three signals merge_branches.py trusts
before it skips a target.  The merge ledger is cross-checked the other way --
an entry that claims a merge the upstream branch does not contain is a lost
fix (reverted, or never pushed), not a reassurance.

Read-only: it fetches the remotes (unless --no-fetch) and never touches the
working tree, the ledger, or any report file.  Every target whose fix is not
found is highlighted at the end and the exit status is 1, so a clean run can
gate a release:

    ./check_fixes_landed.py                     # audit branches_to_merge.txt
    ./check_fixes_landed.py -f security.txt -t "master branch-4.1"
    ./check_fixes_landed.py --no-fetch -q       # only print the problems
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Symlinked into ~/bin like the other tools; find merge_branches.py by the real
# location of this file, not by however it was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from merge_branches import (
    APPROVALS_FILE,
    DEFAULT_TARGETS,
    STRIP_SUFFIXES,
    Backporter,
    Bail,
    Config,
    Git,
    Reports,
    Source,
    parse_branch_file,
    sibling_coverage,
    targets_for,
    warn,
)

# ---------------------------------------------------------------------- states
OK = "ok"
COVERED = "covered"  # a listed sibling branch owns that target
MISSING = "MISSING"  # nothing upstream, nothing in the ledger
STALE = "STALE"      # upstream has the subject but a different patch (an earlier cut?)
LOST = "LOST"        # the ledger claims a merge the target does not contain
UNKNOWN = "UNKNOWN"  # cannot tell: branch gone, ref missing, base unworkable

ATTENTION = {MISSING, STALE, LOST, UNKNOWN}


@dataclass
class TargetVerdict:
    target: str
    state: str
    reason: str = ""


@dataclass
class BranchAudit:
    name: str
    verdicts: list[TargetVerdict] = field(default_factory=list)
    note: str = ""


def needs_attention(audits: list[BranchAudit]) -> list[tuple[str, TargetVerdict]]:
    """Every (branch, verdict) that is not landed -- the highlight list."""
    return [(a.name, v) for a in audits for v in a.verdicts if v.state in ATTENTION]


# ------------------------------------------------------------------ the checks
def contained(bp: Backporter, commit: str, target: str) -> bool:
    return bp.git.ok("merge-base", "--is-ancestor", commit, f"{bp.cfg.upstream}/{target}")


def ledger_check(bp: Backporter, src: Source, target: str) -> tuple[str, str] | None:
    """What the merge ledger has to say about this pair, or None if it is silent.

    The ledger names the exact commit the merge run created on the target, so
    its claim is checkable even when the branch itself is long deleted.
    """
    commit = bp.ledger_commits.get((src.name, target))
    if not commit:
        return None
    if not bp.git.has_ref(f"{commit}^{{commit}}"):
        return UNKNOWN, f"the ledger names {commit[:9]} but that object is not in this repo"
    if contained(bp, commit, target):
        return OK, f"merged as {commit[:9]} (ledger), still on {bp.cfg.upstream}/{target}"
    return LOST, (f"the ledger says this landed as {commit[:9]} but "
                  f"{bp.cfg.upstream}/{target} does not contain it -- reverted, or never pushed?")


def classify(verdict: tuple[str, str] | None, ledger: tuple[str, str] | None) -> tuple[str, str]:
    """Fold already_landed()'s answer and the ledger's claim into one state.

    A contained ledger commit beats a "flag": the flag only says an *earlier
    revision* of the work is on the target, and the ledger commit being there
    proves the current one is too.
    """
    if verdict and verdict[0] == "skip":
        return OK, verdict[1]
    if ledger and ledger[0] == OK:
        return ledger
    if verdict:  # "flag": same subject, a different patch
        return STALE, verdict[1]
    if ledger:
        return ledger
    return MISSING, "no sign of the change upstream"


def resolve_head(bp: Backporter, src: Source) -> str | None:
    """The tip to audit: the local listed branch, else the fork's copy.

    fork_name/fork_head are filled in either way -- already_landed() matches
    cherry-pick trailers against them, and an empty fork_head would grep for
    every trailer there is.
    """
    found = bp.resolve_on_fork(src.name)
    local = bp.git.rev_parse(f"refs/heads/{src.name}^{{commit}}")
    head = local or (found[1] if found else None)
    if found:
        src.fork_name, src.fork_head = found
    elif head:
        # Not on the fork: the local tip is the sha a cherry-pick trailer would name.
        src.fork_head = head
    return head


def _ledger_or(bp: Backporter, src: Source, target: str, why: str) -> TargetVerdict:
    verdict = ledger_check(bp, src, target)
    return TargetVerdict(target, *verdict) if verdict else TargetVerdict(target, UNKNOWN, why)


def audit_source(bp: Backporter, src: Source, default_targets: list[str],
                 listed: list[str]) -> BranchAudit:
    """Check every target this branch routes to for the change itself."""
    git, cfg = bp.git, bp.cfg
    audit = BranchAudit(src.name)
    if src.name in bp.held:
        audit.note = f"held: {bp.held[src.name]}"

    head = resolve_head(bp, src)
    if not head:
        audit.note = "branch not found locally or on the fork"
        planned = targets_for(src.name, "", default_targets, cfg.strip_suffixes)
        covered = sibling_coverage(src.name, planned, listed, cfg.strip_suffixes)
        for target in planned:
            if target in covered:
                audit.verdicts.append(
                    TargetVerdict(target, COVERED, f"left to {covered[target]}"))
            else:
                audit.verdicts.append(
                    _ledger_or(bp, src, target, "cannot verify without the branch"))
        return audit
    src.head = head

    base = bp.pick_base(src)
    if isinstance(base, tuple):
        src.base, src.base_branch = base
        src.commits = git.lines("rev-list", "--reverse", "--no-merges", f"{src.base}..{head}")

    planned = targets_for(src.name, src.base_branch, default_targets, cfg.strip_suffixes)
    covered = sibling_coverage(src.name, planned, listed, cfg.strip_suffixes)
    for target, sibling in covered.items():
        audit.verdicts.append(TargetVerdict(target, COVERED, f"left to {sibling}"))
    todo = [target for target in planned if target not in covered]

    for target in todo:
        if not git.has_ref(f"refs/remotes/{cfg.upstream}/{target}"):
            audit.verdicts.append(
                TargetVerdict(target, UNKNOWN, f"no {cfg.upstream}/{target} ref (fetch it?)"))
        elif base is None:
            audit.verdicts.append(
                _ledger_or(bp, src, target, "cannot work out what it branched from"))
        elif isinstance(base, str):
            # merged:<branch>: the ref sits at or behind an upstream tip, so it
            # carries nothing of its own -- the work reached that branch by
            # another route and its commits cannot be named from this ref.
            merged_into = base.split(":", 1)[1]
            if target == merged_into or contained(bp, head, target):
                audit.verdicts.append(
                    TargetVerdict(target, OK, f"contained in {cfg.upstream}/{target}"))
            else:
                audit.verdicts.append(_ledger_or(
                    bp, src, target,
                    f"the branch is contained in {cfg.upstream}/{merged_into}; its commits "
                    f"cannot be named to check {target} by content"))
        elif not src.commits:
            if target == src.base_branch or contained(bp, head, target):
                audit.verdicts.append(
                    TargetVerdict(target, OK, f"contained in {cfg.upstream}/{target}"))
            else:
                audit.verdicts.append(_ledger_or(
                    bp, src, target,
                    f"carries nothing over {src.base_branch}; nothing to check {target} with"))
        elif len(src.commits) > cfg.max_commits:
            audit.verdicts.append(_ledger_or(
                bp, src, target,
                f"{len(src.commits)} commits over {src.base[:9]} -- the base looks wrong"))
        else:
            state, reason = classify(bp.already_landed(target, src),
                                     ledger_check(bp, src, target))
            audit.verdicts.append(TargetVerdict(target, state, reason))
    return audit


# --------------------------------------------------------------------- output
def format_report(audits: list[BranchAudit], quiet: bool = False) -> str:
    lines: list[str] = []
    for audit in audits:
        if quiet and not any(v.state in ATTENTION for v in audit.verdicts):
            continue
        header = audit.name + (f"    # {audit.note}" if audit.note else "")
        lines.append(header)
        for v in audit.verdicts:
            reason = f"  -- {v.reason}" if v.reason else ""
            lines.append(f"  {v.state:<8} {v.target:<11}{reason}")
    problems = needs_attention(audits)
    lines.append("")
    if not problems:
        lines.append("ALL LANDED: every listed fix is on every target it goes to.")
    else:
        lines.append(f"=== NOT LANDED / NEEDS A LOOK ({len(problems)}) ===")
        for name, v in problems:
            reason = f"  -- {v.reason}" if v.reason else ""
            lines.append(f"  {v.state:<8} {name} -> {v.target}{reason}")
    return "\n".join(lines)


# ------------------------------------------------------------------------ main
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-f", "--file", default="branches_to_merge.txt", type=Path,
                        help="branch list, same file merge_branches.py reads "
                             "(default: %(default)s)")
    parser.add_argument("-t", "--targets", default=" ".join(DEFAULT_TARGETS),
                        help="space separated target branches (default: %(default)s)")
    parser.add_argument("--upstream", default="apache-github",
                        help="remote whose branches are the truth (default: %(default)s)")
    parser.add_argument("--fork-remote", default="origin",
                        help="git remote for the fork (default: %(default)s)")
    parser.add_argument("--fork", default="holdenk/spark",
                        help="fork the branches live on (default: %(default)s)")
    parser.add_argument("--ledger", type=Path, default=Path("branches-already-merged.csv"),
                        help="merge_branches.py's ledger, cross-checked against reality "
                             "(default: %(default)s)")
    parser.add_argument("--hold-list", type=Path, default=Path("HELD_BRANCHES.txt"),
                        help="held branches are annotated, not skipped (default: %(default)s)")
    parser.add_argument("--strip-suffix", action="append", metavar="SUFFIX",
                        help="suffixes ignored when routing a branch name; passing any "
                             "REPLACES the defaults (-aok, -squashed), same as "
                             "merge_branches.py (repeatable)")
    parser.add_argument("--max-commits", type=int, default=30,
                        help="a bigger range means the base guess is wrong, not a big fix "
                             "(default: %(default)s)")
    parser.add_argument("--no-fetch", action="store_true",
                        help="audit the refs already here instead of fetching first")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="only print the branches with problems")
    return parser.parse_args(argv)


def make_config(args: argparse.Namespace) -> Config:
    """An inert Config: the audit may not push, build, prompt or write anything."""
    return Config(
        branch_file=args.file, targets=args.targets.split(), fork_repo=args.fork,
        fork_remote=args.fork_remote, upstream=args.upstream, push_remote=args.upstream,
        do_push=False, skip_ci=True, sanity=False, review_model="", review_timeout=0,
        max_diff_bytes=0, assume_yes=True, max_commits=args.max_commits,
        strip_suffixes=args.strip_suffix or STRIP_SUFFIXES, pick_from_fork=False,
        operator_review=False, ci_shortcut=False, review_diff_lines=0, do_compile=False,
        dry_run=True, sbt=None, sbt_tasks=[], clean_retry=False, sbt_timeout=0,
        log_dir=Path("."), ledger=args.ledger, approvals=Path(APPROVALS_FILE),
        ignore_ledger=False, hold_list=args.hold_list, ignore_hold_list=False,
        jobs=1, ci_poll_minutes=0, ci_wait_hours=0, refresh_before_retry=False,
        local_stand_in=False, update_bases=Path("/nonexistent"),
        squash_magic=Path("/nonexistent"), refresh_timeout=0,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if root.returncode != 0:
        raise Bail("not inside a git repository")
    git = Git(Path(root.stdout.strip()))
    for remote in (args.upstream, args.fork_remote):
        if not git.ok("remote", "get-url", remote):
            raise Bail(f"no remote '{remote}'")
    if not args.no_fetch:
        for remote in (args.upstream, args.fork_remote):
            if not git.ok("fetch", "--quiet", remote):
                warn(f"could not fetch {remote} -- auditing the refs already here")

    cfg = make_config(args)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    bp = Backporter(cfg, git, Reports(stamp, Path.cwd(), dry_run=True), stamp)

    try:
        text = args.file.read_text(encoding="utf-8")
    except OSError as exc:
        raise Bail(f"cannot read {args.file}: {exc}") from exc
    entries = parse_branch_file(text)
    if not entries:
        raise Bail(f"{args.file} contains no branches")
    listed = [name for name, _ in entries]

    # Every configured target is audited: one without an upstream ref gets an
    # UNKNOWN row, not a silent skip -- a gate that passes because a target was
    # never looked at is worse than no gate.
    missing = [t for t in cfg.targets
               if not git.has_ref(f"refs/remotes/{cfg.upstream}/{t}")]
    if missing:
        warn(f"no {cfg.upstream} ref for: {' '.join(missing)} -- those rows come out UNKNOWN")

    print(f"auditing {len(entries)} branch(es) against {cfg.upstream} "
          f"({' '.join(cfg.targets)})")
    audits = [audit_source(bp, Source(name, base), cfg.targets, listed)
              for name, base in entries]
    print(format_report(audits, quiet=args.quiet))
    return 1 if needs_attention(audits) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Bail as exc:
        warn(str(exc))
        sys.exit(1)
    except KeyboardInterrupt:
        warn("interrupted")
        sys.exit(130)
