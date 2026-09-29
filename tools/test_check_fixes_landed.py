#!/usr/bin/env python3
"""Unit tests for check_fixes_landed.py.

    python3 -m unittest test_check_fixes_landed -v   (or: pytest test_check_fixes_landed.py)
"""

import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from check_fixes_landed import (
    COVERED,
    LOST,
    MISSING,
    OK,
    STALE,
    UNKNOWN,
    BranchAudit,
    Source,
    TargetVerdict,
    audit_source,
    classify,
    format_report,
    ledger_check,
    needs_attention,
    resolve_head,
)
from merge_branches import STRIP_SUFFIXES

ALL_TARGETS = ["master", "branch-4.x", "branch-4.1", "branch-3.5"]


class Classify(unittest.TestCase):
    def test_a_content_match_is_landed_whatever_the_ledger_says(self):
        for ledger in (None, (LOST, "ledger lied"), (OK, "ledger agrees")):
            with self.subTest(ledger=ledger):
                self.assertEqual(
                    classify(("skip", "identical patch already there"), ledger),
                    (OK, "identical patch already there"))

    def test_a_flag_is_a_stale_revision_not_a_landed_fix(self):
        state, _ = classify(("flag", "same subject, different patch"), None)
        self.assertEqual(state, STALE)

    def test_a_contained_ledger_commit_beats_a_flag(self):
        # The flag says an *earlier revision* is on the target; the ledger's
        # commit being contained proves the current one is too.
        self.assertEqual(classify(("flag", "same subject, different patch"),
                                  (OK, "merged as abc123456 (ledger)"))[0], OK)

    def test_a_lost_ledger_entry_does_not_mask_a_flag(self):
        self.assertEqual(classify(("flag", "same subject, different patch"),
                                  (LOST, "gone"))[0], STALE)

    def test_no_content_falls_back_to_the_ledger(self):
        self.assertEqual(classify(None, (OK, "merged as abc"))[0], OK)
        self.assertEqual(classify(None, (LOST, "gone"))[0], LOST)

    def test_nothing_anywhere_is_missing(self):
        self.assertEqual(classify(None, None), (MISSING, "no sign of the change upstream"))


class FakeBackporter:
    """A Backporter shell with the git layer stubbed, as in test_merge_branches."""

    def make(self, ledger=None, held=None, contained=(), objects=(), refs=ALL_TARGETS):
        import check_fixes_landed

        bp = object.__new__(check_fixes_landed.Backporter)
        bp.cfg = SimpleNamespace(upstream="up", strip_suffixes=STRIP_SUFFIXES, max_commits=30)
        bp.ledger_commits = ledger or {}
        bp.held = held or {}
        self.contained = set(contained)      # (commit, target) pairs that are ancestors
        self.objects = set(objects)          # shas this clone actually has
        self.refs = list(refs)               # upstream branches that exist
        bp.git = SimpleNamespace(
            rev_parse=lambda ref: None,
            has_ref=lambda ref: self.has_ref(ref),
            ok=lambda *args: (args[0], args[-2], args[-1].split("/", 1)[1]) in self.contained
            if args[0] == "merge-base" else False,
            lines=lambda *args: [],
            out=lambda *args: "",
            subject=lambda rev: f"subject of {rev}",
        )
        bp.resolve_on_fork = lambda name: None
        return bp

    def has_ref(self, ref):
        if ref.startswith("refs/remotes/up/"):
            return ref.removeprefix("refs/remotes/up/") in self.refs
        if ref.endswith("^{commit}"):
            return ref.removesuffix("^{commit}") in self.objects
        return False


class LedgerCheck(FakeBackporter, unittest.TestCase):
    def test_no_entry_is_silence(self):
        bp = self.make()
        self.assertIsNone(ledger_check(bp, Source("f1"), "master"))

    def test_an_entry_whose_commit_we_do_not_have_is_unknown(self):
        bp = self.make(ledger={("f1", "master"): "abc123456"})
        state, reason = ledger_check(bp, Source("f1"), "master")
        self.assertEqual(state, UNKNOWN)
        self.assertIn("not in this repo", reason)

    def test_an_entry_still_on_the_target_is_landed(self):
        bp = self.make(ledger={("f1", "master"): "abc123456"}, objects={"abc123456"},
                       contained={("merge-base", "abc123456", "master")})
        state, reason = ledger_check(bp, Source("f1"), "master")
        self.assertEqual((state, "merged as abc123456" in reason), (OK, True))

    def test_an_entry_the_target_lacks_is_lost(self):
        bp = self.make(ledger={("f1", "master"): "abc123456"}, objects={"abc123456"})
        state, reason = ledger_check(bp, Source("f1"), "master")
        self.assertEqual(state, LOST)
        self.assertIn("does not contain it", reason)


class ResolveHead(FakeBackporter, unittest.TestCase):
    def test_the_local_branch_wins_and_the_fork_still_fills_in(self):
        bp = self.make()
        bp.git.rev_parse = lambda ref: "local1"
        bp.resolve_on_fork = lambda name: ("f1", "fork1")
        src = Source("f1")
        self.assertEqual(resolve_head(bp, src), "local1")
        self.assertEqual((src.fork_name, src.fork_head), ("f1", "fork1"))

    def test_the_fork_is_the_fallback(self):
        bp = self.make()
        bp.resolve_on_fork = lambda name: ("f1", "fork1")
        src = Source("f1")
        self.assertEqual(resolve_head(bp, src), "fork1")
        self.assertEqual(src.fork_head, "fork1")

    def test_neither_is_none(self):
        bp = self.make()
        self.assertIsNone(resolve_head(bp, Source("f1")))

    def test_a_local_only_branch_lends_its_tip_to_the_trailer_match(self):
        # already_landed greps for "cherry picked from commit <fork_head>"; an
        # empty fork_head would match every trailer there is.
        bp = self.make()
        bp.git.rev_parse = lambda ref: "local1"
        src = Source("f1")
        resolve_head(bp, src)
        self.assertEqual(src.fork_head, "local1")


class AuditSource(FakeBackporter, unittest.TestCase):
    def audit(self, bp, name="f1-fix", base=None, listed=None):
        return audit_source(bp, Source(name, base), ALL_TARGETS, listed or [name])

    def test_a_branch_that_is_gone_is_unknown_without_the_ledger(self):
        bp = self.make()
        audit = self.audit(bp)
        self.assertEqual(audit.note, "branch not found locally or on the fork")
        self.assertEqual([v.state for v in audit.verdicts], [UNKNOWN] * len(ALL_TARGETS))

    def test_a_gone_branch_is_still_vouched_for_by_a_contained_ledger_commit(self):
        bp = self.make(ledger={("f1-fix", "master"): "abc"}, objects={"abc"},
                       contained={("merge-base", "abc", "master")})
        audit = self.audit(bp)
        by_target = {v.target: v.state for v in audit.verdicts}
        self.assertEqual(by_target["master"], OK)
        self.assertEqual(by_target["branch-3.5"], UNKNOWN)

    def test_a_gone_pinned_branch_only_asks_about_its_target(self):
        bp = self.make()
        audit = self.audit(bp, name="f1-fix-branch-3.5")
        self.assertEqual([v.target for v in audit.verdicts], ["branch-3.5"])

    def make_landed_bp(self, landed_on, commits=("c1",)):
        """A branch found locally, based on master, with already_landed stubbed."""
        bp = self.make()
        bp.git.rev_parse = lambda ref: "head1"
        bp.git.lines = lambda *args: list(commits) if args[0] == "rev-list" else []
        bp.pick_base = lambda src: ("base1", "master")
        bp.already_landed = lambda target, src: (
            ("skip", "identical patch already there") if target in landed_on else None)
        return bp

    def test_each_target_gets_its_own_answer(self):
        bp = self.make_landed_bp({"master", "branch-4.x"})
        audit = self.audit(bp)
        by_target = {v.target: v.state for v in audit.verdicts}
        self.assertEqual(by_target["master"], OK)
        self.assertEqual(by_target["branch-4.x"], OK)
        self.assertEqual(by_target["branch-4.1"], MISSING)
        self.assertEqual(by_target["branch-3.5"], MISSING)

    def test_a_listed_sibling_covers_its_target(self):
        bp = self.make_landed_bp({"master", "branch-4.x", "branch-4.1"})
        listed = ["f1-fix", "f1-fix-branch-3.5"]
        audit = self.audit(bp, listed=listed)
        covered = [v for v in audit.verdicts if v.state == COVERED]
        self.assertEqual([(v.target, v.reason) for v in covered],
                         [("branch-3.5", "left to f1-fix-branch-3.5")])

    def test_a_target_without_an_upstream_ref_is_unknown(self):
        bp = self.make_landed_bp({"master"})
        bp.git.has_ref = lambda ref: ref != "refs/remotes/up/branch-3.5" and self.has_ref(ref)
        audit = self.audit(bp)
        by_target = {v.target: v.state for v in audit.verdicts}
        self.assertEqual(by_target["branch-3.5"], UNKNOWN)
        self.assertEqual(by_target["master"], OK)

    def test_a_merged_branch_is_landed_where_it_is_contained(self):
        bp = self.make(contained={("merge-base", "head1", "branch-3.5")})
        bp.git.rev_parse = lambda ref: "head1"
        bp.pick_base = lambda src: "merged:master"
        audit = self.audit(bp)
        by_target = {v.target: v.state for v in audit.verdicts}
        self.assertEqual(by_target["master"], OK)        # merged into it
        self.assertEqual(by_target["branch-3.5"], OK)    # contained there too
        # not contained and no way to name the commits: honestly unknown
        self.assertEqual(by_target["branch-4.x"], UNKNOWN)
        self.assertEqual(by_target["branch-4.1"], UNKNOWN)

    def test_a_merged_branch_still_trusts_the_ledger_elsewhere(self):
        bp = self.make(ledger={("f1-fix", "branch-4.1"): "abc"}, objects={"abc"},
                       contained={("merge-base", "abc", "branch-4.1")})
        bp.git.rev_parse = lambda ref: "head1"
        bp.pick_base = lambda src: "merged:master"
        audit = self.audit(bp)
        by_target = {v.target: v.state for v in audit.verdicts}
        self.assertEqual(by_target["branch-4.1"], OK)

    def test_an_unworkable_base_is_unknown_not_missing(self):
        bp = self.make()
        bp.git.rev_parse = lambda ref: "head1"
        bp.pick_base = lambda src: None
        audit = self.audit(bp)
        self.assertEqual([v.state for v in audit.verdicts], [UNKNOWN] * len(ALL_TARGETS))

    def test_too_many_commits_means_the_base_is_wrong_not_the_fix_missing(self):
        bp = self.make_landed_bp(set(), commits=tuple(f"c{i}" for i in range(31)))
        audit = self.audit(bp)
        self.assertTrue(all(v.state == UNKNOWN for v in audit.verdicts))
        self.assertIn("base looks wrong", audit.verdicts[0].reason)

    def test_a_gone_branch_leaves_a_siblings_target_to_the_sibling(self):
        bp = self.make()
        listed = ["f1-fix", "f1-fix-branch-3.5"]
        audit = self.audit(bp, listed=listed)
        by_target = {v.target: v.state for v in audit.verdicts}
        self.assertEqual(by_target["branch-3.5"], COVERED)
        self.assertEqual(by_target["master"], UNKNOWN)

    def test_a_branch_with_no_commits_of_its_own_carries_nothing(self):
        # A tuple base with an empty range: the rebase that cut the branch
        # dropped every commit as already applied.
        bp = self.make()
        bp.git.rev_parse = lambda ref: "head1"
        bp.git.lines = lambda *args: []
        bp.pick_base = lambda src: ("base1", "master")
        audit = self.audit(bp)
        by_target = {v.target: v.state for v in audit.verdicts}
        self.assertEqual(by_target["master"], OK)      # its base holds the work
        self.assertEqual(by_target["branch-3.5"], UNKNOWN)

    def test_a_two_commit_branch_is_checked_per_target(self):
        bp = self.make_landed_bp({"master"}, commits=("c1", "c2"))
        audit = self.audit(bp)
        by_target = {v.target: v.state for v in audit.verdicts}
        self.assertEqual(by_target["master"], OK)
        self.assertEqual(by_target["branch-3.5"], MISSING)

    def test_a_held_branch_says_so_and_is_audited_anyway(self):
        bp = self.make_landed_bp({"master"})
        bp.held = {"f1-fix": "waiting on the reporter"}
        audit = self.audit(bp)
        self.assertEqual(audit.note, "held: waiting on the reporter")
        self.assertIn(MISSING, [v.state for v in audit.verdicts])


class Report(unittest.TestCase):
    def audit(self, name, *verdicts):
        return BranchAudit(name, [TargetVerdict(t, s, r) for t, s, r in verdicts])

    def test_a_clean_run_says_so(self):
        audits = [self.audit("f1", ("master", OK, "")), self.audit("f2", ("master", OK, ""))]
        self.assertEqual(needs_attention(audits), [])
        self.assertIn("ALL LANDED", format_report(audits))

    def test_problems_are_highlighted_with_their_reasons(self):
        audits = [
            self.audit("f1", ("master", OK, "")),
            self.audit("f2", ("branch-3.5", MISSING, "no sign of the change upstream")),
        ]
        report = format_report(audits)
        self.assertIn("NOT LANDED / NEEDS A LOOK (1)", report)
        self.assertIn("MISSING  f2 -> branch-3.5  -- no sign of the change upstream", report)
        self.assertEqual([(n, v.target) for n, v in needs_attention(audits)],
                         [("f2", "branch-3.5")])

    def test_quiet_hides_the_clean_branches_but_not_the_summary(self):
        audits = [
            self.audit("f1", ("master", OK, "")),
            self.audit("f2", ("master", LOST, "never pushed")),
        ]
        report = format_report(audits, quiet=True)
        self.assertNotIn("\nf1\n", "\n" + report + "\n")
        self.assertIn("f2", report)
        self.assertIn("LOST", report)

    def test_covered_is_not_a_problem(self):
        audits = [self.audit("f1", ("branch-3.5", COVERED, "left to f1-branch-3.5"))]
        self.assertEqual(needs_attention(audits), [])


class Integration(unittest.TestCase):
    """main() end to end against a real (throwaway) pair of repositories."""

    def setUp(self):
        import shutil
        import subprocess
        import tempfile

        if not shutil.which("git"):
            self.skipTest("git is not available")
        self.subprocess = subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "work"
        self.git("init", "--bare", "-q", "-b", "master", str(self.root / "upstream.git"),
                 cwd=self.root)
        self.git("init", "-q", "-b", "master", str(self.repo), cwd=self.root)
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        self.git("remote", "add", "origin", str(self.root / "upstream.git"))

        # f1-fix: cut from master, landed on master as a cherry-pick (new sha,
        # because master moved on first), not yet on branch-3.5.
        (self.repo / "f.txt").write_text("base\n")
        self.git("add", "f.txt")
        self.git("commit", "-qm", "initial commit with a reasonably long subject")
        self.git("branch", "branch-3.5")
        self.git("checkout", "-qb", "f1-fix")
        (self.repo / "fix1.txt").write_text("fix1\n")
        self.git("add", "fix1.txt")
        self.git("commit", "-qm", "f1 tighten the frobnicate option validation")
        self.git("checkout", "-q", "master")
        with (self.repo / "f.txt").open("a") as handle:
            handle.write("m1\n")
        self.git("commit", "-qam", "master only work with a long enough subject")
        self.git("cherry-pick", "f1-fix")
        # f2-fix-branch-3.5: exists locally, landed nowhere.
        self.git("checkout", "-qb", "f2-fix-branch-3.5")
        (self.repo / "fix2.txt").write_text("fix2\n")
        self.git("add", "fix2.txt")
        self.git("commit", "-qm", "f2 tighten the other thing validation")
        self.git("checkout", "-q", "master")
        self.git("push", "-q", "origin", "master", "branch-3.5")

        self.branches = self.root / "branches.txt"
        self.branches.write_text("f1-fix\nf2-fix-branch-3.5\n")
        self.ledger = self.root / "ledger.csv"
        cwd = Path.cwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.repo)

    def git(self, *args, cwd=None):
        return self.subprocess.run(["git", *args], cwd=cwd or self.repo, check=True,
                                   text=True, capture_output=True)

    def run_audit(self):
        import contextlib
        import io

        from check_fixes_landed import main

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--no-fetch", "--upstream", "origin", "--fork-remote", "origin",
                         "-t", "master branch-3.5", "-f", str(self.branches),
                         "--ledger", str(self.ledger)])
        return code, out.getvalue()

    def test_missing_fixes_are_highlighted_and_fail_the_run(self):
        code, report = self.run_audit()
        self.assertEqual(code, 1)
        self.assertIn("ok       master", report)
        self.assertIn("MISSING  branch-3.5", report)
        self.assertIn("NOT LANDED / NEEDS A LOOK (2)", report)
        self.assertIn("MISSING  f2-fix-branch-3.5 -> branch-3.5", report)

    def test_a_ledger_entry_the_target_lacks_is_lost(self):
        master_tip = self.git("rev-parse", "origin/master").stdout.strip()
        self.git("branch", "-q", "-D", "f2-fix-branch-3.5")
        self.ledger.write_text(
            "merged_at,branch,target,commit,source_head,pushed_to\n"
            f"2026-09-29,f2-fix-branch-3.5,branch-3.5,{master_tip},deadbeef,apache\n")
        code, report = self.run_audit()
        self.assertEqual(code, 1)
        self.assertIn("LOST", report)
        self.assertIn("does not contain it", report)

    def test_everything_landed_exits_clean(self):
        self.git("checkout", "-q", "branch-3.5")
        # -x: a distinct sha even when the whole test runs inside one second
        # (an identical tree+parent+message+timestamp cherry-pick is the SAME
        # commit), and the trailer path gets exercised on the way.
        self.git("cherry-pick", "-x", "f1-fix")
        self.git("push", "-q", "origin", "branch-3.5")
        self.git("checkout", "-q", "master")
        self.branches.write_text("f1-fix\n")
        code, report = self.run_audit()
        self.assertEqual(code, 0)
        self.assertIn("ALL LANDED", report)


if __name__ == "__main__":
    unittest.main()
