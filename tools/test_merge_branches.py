#!/usr/bin/env python3
"""Unit tests for the pure rules in merge_branches.py.

    python3 -m unittest test_merge_branches -v      (or: pytest test_merge_branches.py)
"""

import csv
import tempfile
import unittest
from pathlib import Path

from merge_branches import (
    APPROVAL_COLUMNS,
    LEDGER_COLUMNS,
    Source,
    ci_status,
    family_stem,
    is_specific_subject,
    load_approvals,
    load_hold_list,
    load_ledger,
    logical_name,
    parse_branch_file,
    parse_verdict,
    ref_slug,
    sibling_coverage,
    squash_output,
    suffix_target,
    targets_for,
)

ALL_TARGETS = ["master", "branch-4.x", "branch-4.1", "branch-4.2", "branch-3.5"]


class SuffixTarget(unittest.TestCase):
    def test_version_endings_pin_a_single_branch(self):
        cases = {
            "some-fix-master": "master",
            "xss-fix-branch-3.5": "branch-3.5",
            "release-4.0": "branch-4.0",
            "SPARK-1234-fix-4.1": "branch-4.1",
            "xss-fix-branch-4.2": "branch-4.2",
            "my-branch-4.3": "branch-4.3",
            "xss-fix-branch-4.x": "branch-4.x",
            "thing-4x": "branch-4.x",
        }
        for branch, expected in cases.items():
            with self.subTest(branch=branch):
                self.assertEqual(suffix_target(branch), expected)

    def test_a_version_that_is_not_the_ending_does_not_pin(self):
        for branch in ("3.5-cve-fixes", "master-of-none", "fix-4.1-followup", "SPARK-59119-cyclic-cause_idx",
                       "branch-4.10", "no-version-here"):
            with self.subTest(branch=branch):
                self.assertIsNone(suffix_target(branch))

    def test_every_spelling_of_the_same_pin_agrees(self):
        """-4x, -4.x, with or without "-branch", with or without a revision."""
        stem = "f091-kafka-datasource-option-restrict"
        spellings = [f"{stem}{tail}" for tail in (
            "-4x", "-4.x", "-branch-4x", "-branch-4.x",
            "-4x-aok", "-branch-4x-squashed", "-branch-4.x-aok",
            "-4.x-r2", "-branch-4x-r2", "-branch-4.x-r2-aok",
        )]
        for branch in spellings:
            with self.subTest(branch=branch):
                self.assertEqual(suffix_target(branch), "branch-4.x")
                self.assertEqual(family_stem(branch), stem)

    def test_a_revision_marker_does_not_hide_the_version(self):
        # SPARK-59118-doAs-warn-4.x-r2 is the second cut for 4.x, not an
        # unpinned branch that would go to every release.
        self.assertEqual(suffix_target("SPARK-59118-doAs-warn-4.x-r2"), "branch-4.x")
        self.assertEqual(family_stem("SPARK-59118-doAs-warn-4.x-r2"),
                         "SPARK-59118-doAs-warn")

    def test_a_revision_marker_on_an_unpinned_name_is_part_of_the_name(self):
        # f003-r2 is the name of the work; there is no f003 to fold it into.
        self.assertIsNone(suffix_target("f003-r2-aok"))
        self.assertEqual(family_stem("f003-r2-aok"), "f003-r2")
        self.assertEqual(family_stem("f003-r2-branch-3.5-aok"), "f003-r2")
        self.assertEqual(suffix_target("f003-r2-branch-3.5-aok"), "branch-3.5")


class LogicalName(unittest.TestCase):
    def test_strips_the_squash_marker(self):
        self.assertEqual(logical_name("security-agg-injection-branch-4.2-aok"),
                         "security-agg-injection-branch-4.2")
        self.assertEqual(logical_name("ldap-thriftserver-improvement-squashed"),
                         "ldap-thriftserver-improvement")
        self.assertEqual(logical_name("some-fix-4.1-squashed"), "some-fix-4.1")

    def test_strips_stacked_markers(self):
        self.assertEqual(logical_name("thing-squashed-aok"), "thing")
        self.assertEqual(logical_name("thing-aok-squashed"), "thing")

    def test_leaves_an_unmarked_name_alone(self):
        self.assertEqual(logical_name("f107-improve-show-create-table-escaping"),
                         "f107-improve-show-create-table-escaping")

    def test_strips_repeats_but_never_the_whole_name(self):
        self.assertEqual(logical_name("thing-aok-aok"), "thing")
        self.assertEqual(logical_name("-aok"), "-aok")

    def test_the_marker_must_be_at_the_end(self):
        self.assertEqual(logical_name("aok-thing"), "aok-thing")

    def test_custom_suffixes(self):
        self.assertEqual(logical_name("thing-wip", ["-wip"]), "thing")
        self.assertEqual(logical_name("thing-aok", ["-wip"]), "thing-aok")


class SquashedBranchRouting(unittest.TestCase):
    """The -aok rewrites keep the original name plus a marker."""

    def test_version_is_found_behind_the_marker(self):
        self.assertEqual(suffix_target("security-agg-injection-branch-4.2-aok"), "branch-4.2")
        self.assertEqual(suffix_target("security-agg-injection-branch-4.2-squashed"),
                         "branch-4.2")
        self.assertEqual(suffix_target("udt-config-opt-squashed"), None)
        self.assertEqual(suffix_target("some-fix-master-aok"), "master")
        self.assertEqual(suffix_target("xss-fix-branch-4.x-aok"), "branch-4.x")

    def test_a_marked_branch_with_no_version_is_not_pinned(self):
        self.assertIsNone(suffix_target("f107-improve-show-create-table-escaping-aok"))
        self.assertIsNone(suffix_target("f007-mergedir-aok"))

    def test_routing_uses_the_stripped_name(self):
        self.assertEqual(
            targets_for("security-agg-injection-branch-4.2-aok", "master", ALL_TARGETS),
            ["branch-4.2"])
        self.assertEqual(
            targets_for("f007-mergedir-aok", "master", ALL_TARGETS), ALL_TARGETS)


class TargetsFor(unittest.TestCase):
    def test_name_pins_the_target(self):
        self.assertEqual(targets_for("fix-4.1", "master", ALL_TARGETS), ["branch-4.1"])

    def test_cut_from_35_stays_on_35(self):
        self.assertEqual(targets_for("some-fix", "branch-3.5", ALL_TARGETS), ["branch-3.5"])

    def test_name_beats_the_base(self):
        self.assertEqual(targets_for("some-fix-4.2", "branch-3.5", ALL_TARGETS), ["branch-4.2"])
        self.assertEqual(targets_for("some-fix-master", "branch-3.5", ALL_TARGETS), ["master"])

    def test_master_suffix_pins_to_master_only(self):
        self.assertEqual(targets_for("f003-r2-master", "master", ALL_TARGETS), ["master"])

    def test_otherwise_everything(self):
        self.assertEqual(targets_for("some-fix", "master", ALL_TARGETS), ALL_TARGETS)

    def test_the_default_list_is_not_aliased(self):
        got = targets_for("some-fix", "master", ALL_TARGETS)
        got.append("branch-9.9")
        self.assertNotIn("branch-9.9", ALL_TARGETS)


class CiStatus(unittest.TestCase):
    def test_no_runs_is_none(self):
        self.assertEqual(ci_status([]), "none")

    def test_anything_unfinished_is_running(self):
        self.assertEqual(ci_status([("completed", "success"), ("queued", "")]), "running")
        self.assertEqual(ci_status([("in_progress", "")]), "running")

    def test_a_failure_among_finished_runs_is_failing(self):
        self.assertEqual(ci_status([("completed", "success"), ("completed", "failure")]), "failing")
        for bad in ("timed_out", "cancelled", "startup_failure", "action_required", "error"):
            with self.subTest(conclusion=bad):
                self.assertEqual(ci_status([("completed", bad)]), "failing")

    def test_success_is_passing_even_next_to_skipped(self):
        self.assertEqual(ci_status([("completed", "skipped"), ("completed", "success")]), "passing")

    def test_all_skipped_is_not_a_pass(self):
        self.assertEqual(ci_status([("completed", "skipped")]), "none")

    def test_running_wins_over_a_failure_but_says_so(self):
        # A branch still building is "come back later", not "rejected" -- but an
        # already-failed job should not be invisible while the rest queues.
        self.assertEqual(ci_status([("completed", "failure"), ("queued", "")]),
                         "running-with-failures")

    def test_a_clean_pending_run_is_plain_running(self):
        self.assertEqual(ci_status([("completed", "success"), ("queued", "")]), "running")


class ParseBranchFile(unittest.TestCase):
    def test_names_comments_and_blanks(self):
        text = "alpha\n\n# a comment\nbeta   # trailing comment\n"
        self.assertEqual(parse_branch_file(text), [("alpha", None), ("beta", None)])

    def test_optional_base_column(self):
        self.assertEqual(parse_branch_file("alpha branch-3.5\n"), [("alpha", "branch-3.5")])

    def test_last_line_without_a_newline_still_counts(self):
        self.assertEqual(parse_branch_file("alpha"), [("alpha", None)])

    def test_a_branch_listed_twice_is_taken_once(self):
        self.assertEqual(parse_branch_file("alpha\nbeta\nalpha\n"),
                         [("alpha", None), ("beta", None)])

    def test_extra_columns_are_ignored(self):
        self.assertEqual(parse_branch_file("alpha master junk\n"), [("alpha", "master")])


class ParseVerdict(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(parse_verdict("VERDICT: OK"), ("OK", ""))

    def test_questionable_with_a_reason(self):
        verdict, reason = parse_verdict("VERDICT: QUESTIONABLE - deletes a test suite")
        self.assertEqual(verdict, "QUESTIONABLE")
        self.assertEqual(reason, "deletes a test suite")

    def test_verdict_buried_in_prose(self):
        verdict, reason = parse_verdict("Looking at this diff...\nVERDICT: QUESTIONABLE - risky\n")
        self.assertEqual((verdict, reason), ("QUESTIONABLE", "risky"))

    def test_empty_answer_is_unknown(self):
        self.assertEqual(parse_verdict("")[0], "UNKNOWN")
        self.assertEqual(parse_verdict("   \n")[0], "UNKNOWN")

    def test_unparseable_answer_is_unknown_and_quotes_what_came_back(self):
        verdict, reason = parse_verdict("I think it is fine, honestly")
        self.assertEqual(verdict, "UNKNOWN")
        self.assertIn("I think it is fine", reason)


class SpecificSubject(unittest.TestCase):
    """Subject matching only means something for a subject that says something."""

    def test_a_real_commit_subject_is_specific(self):
        self.assertTrue(is_specific_subject(
            "[SPARK-59200][DOCS] Clarify the spark.eventLog.compress description"))

    def test_a_generic_subject_is_not(self):
        for subject in ("[MINOR] Fix typo", "fix", "  Update docs  ", ""):
            with self.subTest(subject=subject):
                self.assertFalse(is_specific_subject(subject))


class Families(unittest.TestCase):
    """A change that will not apply everywhere arrives as a plain branch plus a
    per-release cut of the same work."""

    def test_stem_strips_markers_and_version(self):
        for name in ("f107-fix", "f107-fix-aok", "f107-fix-3.5", "f107-fix-3.5-aok",
                     "f107-fix-4.1-squashed", "f107-fix-master",
                     # the other spelling: -branch-3.5 rather than -3.5
                     "f107-fix-branch-3.5", "f107-fix-branch-4.x-aok",
                     "f107-fix-branch-master"):
            with self.subTest(name=name):
                self.assertEqual(family_stem(name), "f107-fix")

    def test_mixed_spellings_pair_with_the_plain_branch(self):
        listed = ["f091-kafka-datasource-option-restrict",
                  "f091-kafka-datasource-option-restrict-4x",
                  "f091-kafka-datasource-option-restrict-branch-3.5-aok"]
        self.assertEqual(
            sibling_coverage("f091-kafka-datasource-option-restrict", ALL_TARGETS, listed),
            {"branch-4.x": "f091-kafka-datasource-option-restrict-4x",
             "branch-3.5": "f091-kafka-datasource-option-restrict-branch-3.5-aok"})

    def test_a_name_that_really_ends_in_branch_keeps_it(self):
        # only the "-branch" that came in front of a version is dropped
        self.assertEqual(family_stem("security-agg-injection-branch"),
                         "security-agg-injection-branch")

    def test_the_branch_spelling_pairs_with_the_plain_one(self):
        listed = ["f040-compressed-file-handling",
                  "f040-compressed-file-handling-branch-3.5",
                  "f040-compressed-file-handling-branch-4.1"]
        self.assertEqual(
            sibling_coverage("f040-compressed-file-handling", ALL_TARGETS, listed),
            {"branch-3.5": "f040-compressed-file-handling-branch-3.5",
             "branch-4.1": "f040-compressed-file-handling-branch-4.1"})

    def test_the_plain_branch_leaves_35_to_its_sibling(self):
        listed = ["f107-fix-aok", "f107-fix-3.5-aok"]
        self.assertEqual(
            sibling_coverage("f107-fix-aok", ALL_TARGETS, listed),
            {"branch-3.5": "f107-fix-3.5-aok"})

    def test_a_pinned_branch_claims_nothing(self):
        listed = ["f107-fix-aok", "f107-fix-3.5-aok"]
        self.assertEqual(sibling_coverage("f107-fix-3.5-aok", ALL_TARGETS, listed), {})

    def test_an_unrelated_branch_is_not_family(self):
        listed = ["f107-fix-aok", "other-thing-3.5-aok"]
        self.assertEqual(sibling_coverage("f107-fix-aok", ALL_TARGETS, listed), {})

    def test_several_siblings_at_once(self):
        listed = ["thing-aok", "thing-3.5-aok", "thing-4.1-aok"]
        self.assertEqual(
            sibling_coverage("thing-aok", ALL_TARGETS, listed),
            {"branch-3.5": "thing-3.5-aok", "branch-4.1": "thing-4.1-aok"})

    def test_a_sibling_for_a_target_not_in_play_is_ignored(self):
        listed = ["thing-aok", "thing-4.3-aok"]
        self.assertEqual(sibling_coverage("thing-aok", ALL_TARGETS, listed), {})


class Ledger(unittest.TestCase):
    def ledger(self, rows):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
        writer = csv.writer(handle)
        writer.writerow(LEDGER_COLUMNS)
        writer.writerows(rows)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def test_missing_file_is_no_history(self):
        self.assertEqual(load_ledger(Path("/nonexistent/ledger.csv")), set())

    def test_reads_branch_and_target_pairs(self):
        path = self.ledger([
            ["2026-08-31T10:00:00-07:00", "f003-r2-aok", "master", "abc", "def", "apache"],
            ["2026-08-31T10:05:00-07:00", "f003-r2-aok", "branch-4.1", "ghi", "def", "apache"],
        ])
        self.assertEqual(load_ledger(path),
                         {("f003-r2-aok", "master"), ("f003-r2-aok", "branch-4.1")})

    def test_blank_and_ragged_rows_are_ignored(self):
        path = self.ledger([
            ["2026-08-31T10:00:00-07:00", "good-aok", "master", "abc", "def", "apache"],
            ["2026-08-31T10:00:00-07:00", "", "master", "", "", ""],
            ["2026-08-31T10:00:00-07:00", "no-target", "", "", "", ""],
        ])
        self.assertEqual(load_ledger(path), {("good-aok", "master")})

    def test_a_subject_with_a_comma_survives_the_round_trip(self):
        path = self.ledger([["t", "branch,with,commas", "master", "a", "b", "apache"]])
        self.assertEqual(load_ledger(path), {("branch,with,commas", "master")})


class HoldList(unittest.TestCase):
    def hold_file(self, body):
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        handle.write(body)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def test_missing_file_holds_nothing(self):
        self.assertEqual(load_hold_list(Path("/nonexistent/hold.txt")), {})

    def test_reads_names_and_reasons_and_skips_comments(self):
        path = self.hold_file(
            "# branches on hold\n"
            "foo-aok\tflaky test, chasing it\n"
            "bar-aok\n")
        held = load_hold_list(path)
        self.assertEqual(set(held), {"foo-aok", "bar-aok"})
        self.assertEqual(held["foo-aok"], "flaky test, chasing it")
        self.assertEqual(held["bar-aok"], "listed")

    def test_the_first_reason_wins_for_a_repeated_branch(self):
        path = self.hold_file("foo-aok\tabc\tfirst reason\nfoo-aok\tdef\tlater reason\n")
        self.assertEqual(load_hold_list(path)["foo-aok"], "first reason")


class Approvals(unittest.TestCase):
    """Approvals are keyed by the content, so a re-pushed branch is asked about
    again rather than waved through."""

    def approvals(self, rows):
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, newline="")
        handle.write("# run whenever\n")
        handle.write("# " + "\t".join(APPROVAL_COLUMNS) + "\n")
        for row in rows:
            handle.write("\t".join(row) + "\n")
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def test_missing_file_is_no_approvals(self):
        self.assertEqual(load_approvals(Path("/nonexistent/approved.txt")), set())

    def test_reads_branch_and_patch_id(self):
        path = self.approvals([
            ["2026-09-01T01:00:00-07:00", "foo-aok", "abc", "patch111", "master"],
            ["2026-09-01T01:00:01-07:00", "bar-aok", "def", "patch222", "master branch-4.1"],
        ])
        self.assertEqual(load_approvals(path),
                         {("foo-aok", "patch111"), ("bar-aok", "patch222")})

    def test_a_row_without_a_patch_id_is_ignored(self):
        path = self.approvals([["t", "foo-aok", "abc", "", "master"]])
        self.assertEqual(load_approvals(path), set())


class FailingCiGetsOneRetry(unittest.TestCase):
    """A red build is held back and looked at once more at the very end."""

    def make(self, states, heads=None):
        """A Backporter with everything retry_failed() touches stubbed out."""
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(dry_run=False, fork_remote="fork",
                                 fork_repo="holdenk/spark", ci_poll_minutes=0,
                                 ci_wait_hours=0, refresh_before_retry=False)
        bp.git = SimpleNamespace(run=lambda *a, **k: None)
        self.written = []
        self.recorded = []
        self.merged = []
        bp.reports = SimpleNamespace(write=lambda name, *cols: self.written.append((name, cols)))
        bp.record = lambda name, target, why: self.recorded.append((name, why))
        bp.warn_if_untested = lambda src: None
        bp.merge_all = lambda work: self.merged.extend(name for (src, _) in work
                                                       for name in [src.name])
        bp.ci_status_of = lambda sha: states[sha]
        heads = heads or {}
        bp.resolve_on_fork = lambda name: heads.get(name, (name, name + "-head"))
        return bp

    def source(self, name):
        src = Source(name)
        src.fork_name, src.fork_head = name, name + "-head"
        return src

    def test_green_on_the_second_look_is_merged(self):
        bp = self.make({"a-head": "passing"})
        bp.retry_failed([(self.source("a"), ["master"])])
        self.assertEqual(self.merged, ["a"])
        self.assertEqual(self.written, [])

    def test_still_red_on_the_second_look_is_written_off(self):
        bp = self.make({"a-head": "failing"})
        bp.retry_failed([(self.source("a"), ["master"])])
        self.assertEqual(self.merged, [])
        self.assertEqual([name for name, _ in self.written], ["FAILING_CI.txt"])

    def test_the_extra_shot_is_spent_even_if_it_is_building_again(self):
        # a re-run puts the commit back to "running"; that is not a second
        # failure, but it is not another free look either
        bp = self.make({"a-head": "running"})
        bp.retry_failed([(self.source("a"), ["master"])])
        self.assertEqual(self.merged, [])
        self.assertEqual([name for name, _ in self.written], ["TO_MERGE_LATER.txt"])

    def test_a_new_push_is_left_for_the_next_run(self):
        bp = self.make({}, heads={"a": ("a", "different-head")})
        bp.retry_failed([(self.source("a"), ["master"])])
        self.assertEqual(self.merged, [])
        self.assertEqual([name for name, _ in self.written], ["TO_MERGE_LATER.txt"])

    def test_nothing_to_retry_asks_github_nothing(self):
        bp = self.make({})
        bp.retry_failed([])
        self.assertEqual((self.merged, self.written, self.recorded), ([], [], []))

    def test_the_rewrites_are_re_cut_before_the_second_look(self):
        bp = self.make({"a-head": "passing"})
        bp.cfg.refresh_before_retry = True
        seen = []
        bp.refresh_rewrites = lambda work: (seen.append([src.name for src, _ in work])
                                            or work)
        bp.retry_failed([(self.source("a"), ["master"])])
        self.assertEqual(seen, [["a"]])          # and before anything was merged
        self.assertEqual(self.merged, ["a"])

    def test_a_branch_that_survives_nothing_after_re_cutting_stops_there(self):
        bp = self.make({"a-head": "passing"})
        bp.cfg.refresh_before_retry = True
        bp.refresh_rewrites = lambda work: []
        bp.retry_failed([(self.source("a"), ["master"])])
        self.assertEqual((self.merged, self.written), ([], []))

    def test_revisit_hands_a_late_failure_on_instead_of_filing_it(self):
        bp = self.make({"a-head": "failing"})
        late = bp.revisit([(self.source("a"), ["master"])])
        self.assertEqual([src.name for src, _ in late], ["a"])
        self.assertEqual(self.written, [])   # retry_failed decides, not revisit


class SquashOutput(unittest.TestCase):
    def test_the_rewrites_are_found_by_the_name_they_share(self):
        made = squash_output("f1-aok\nf2-squashed\n\n# a comment\n")
        self.assertEqual(made, {"f1": "f1-aok", "f2": "f2-squashed"})

    def test_a_branch_that_came_back_under_a_different_marker_still_matches(self):
        # listed as f1-aok, but a second commit turned up and it is -squashed now
        self.assertEqual(squash_output("f1-squashed\n").get(logical_name("f1-aok")),
                         "f1-squashed")


class RefreshBeforeRetry(unittest.TestCase):
    """The -aok/-squashed rewrites are cut again before the last look."""

    def make(self, heads, cut="", helpers=True):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bp.stamp = "stamp"
        bp.cfg = SimpleNamespace(
            refresh_before_retry=True, pick_from_fork=False, dry_run=False,
            strip_suffixes=merge_branches.STRIP_SUFFIXES, upstream="up", fork_remote="fork",
            log_dir=Path(self.tmp.name), update_bases=Path("update-bases.sh"),
            squash_magic=Path("squash-magic.sh"), refresh_timeout=60)
        bp.git = SimpleNamespace(rev_parse=lambda ref: heads.get(
            ref.removeprefix("refs/heads/").removesuffix("^{commit}")))
        self.ran = []
        self.planned = []

        def run_helper(script, args, env):
            self.ran.append((script.name, list(args)))
            if script.name == "squash-magic.sh":
                (Path(self.tmp.name) / "stamp-refresh-branches.txt").write_text(cut)
            return helpers

        bp.run_helper = run_helper
        bp.plan = lambda src: (self.planned.append(src.name) or ["master"])
        return bp

    def source(self, name, pick, head):
        src = Source(name)
        src.pick_name, src.head = pick, head
        return src

    def work(self, src):
        return [(src, ["master", "branch-3.5"])]

    def test_an_unchanged_rewrite_is_left_where_it_was(self):
        bp = self.make({"f1-aok": "old"}, cut="f1-aok\n")
        src = self.source("f1-aok", "f1-aok", "old")
        out = bp.refresh_rewrites(self.work(src))
        self.assertEqual([name for name, _ in self.ran],
                         ["update-bases.sh", "squash-magic.sh"])
        self.assertEqual(out[0][1], ["master", "branch-3.5"])   # not planned again
        self.assertEqual((src.pick_name, src.head, self.planned), ("f1-aok", "old", []))

    def test_squash_magic_is_asked_only_about_the_branches_being_retried(self):
        bp = self.make({"f1-aok": "old"}, cut="f1-aok\n")
        bp.refresh_rewrites(self.work(self.source("f1-aok", "f1-aok", "old")))
        self.assertEqual(dict(self.ran)["squash-magic.sh"], ["f1"])

    def test_a_re_cut_rewrite_is_picked_from_and_planned_again(self):
        bp = self.make({"f1-aok": "new"}, cut="f1-aok\n")
        src = self.source("f1-aok", "f1-aok", "old")
        out = bp.refresh_rewrites(self.work(src))
        self.assertEqual((src.pick_name, src.head), ("f1-aok", "new"))
        self.assertEqual((self.planned, out[0][1]), (["f1-aok"], ["master"]))

    def test_a_rewrite_that_came_back_squashed_is_followed(self):
        bp = self.make({"f1-aok": "old", "f1-squashed": "new"}, cut="f1-squashed\n")
        src = self.source("f1-aok", "f1-aok", "old")
        bp.refresh_rewrites(self.work(src))
        # the listed name is what the reports and the ledger keep calling it
        self.assertEqual((src.name, src.pick_name, src.head),
                         ("f1-aok", "f1-squashed", "new"))

    def test_nothing_left_to_merge_drops_the_branch(self):
        bp = self.make({"f1-aok": "new"}, cut="f1-aok\n")
        bp.plan = lambda src: None
        self.assertEqual(bp.refresh_rewrites(self.work(self.source("f1-aok", "f1-aok", "old"))),
                         [])

    def test_a_helper_that_fails_leaves_the_rewrites_alone(self):
        bp = self.make({"f1-aok": "new"}, cut="f1-aok\n", helpers=False)
        src = self.source("f1-aok", "f1-aok", "old")
        out = bp.refresh_rewrites(self.work(src))
        self.assertEqual([name for name, _ in self.ran], ["update-bases.sh"])
        self.assertEqual((src.head, out[0][1]), ("old", ["master", "branch-3.5"]))

    def test_a_missing_local_rewrite_leaves_the_branch_where_it_was(self):
        bp = self.make({}, cut="f1-aok\n")
        src = self.source("f1-aok", "f1-aok", "old")
        out = bp.refresh_rewrites(self.work(src))
        self.assertEqual((src.head, self.planned, len(out)), ("old", [], 1))

    def test_a_branch_squash_magic_gave_up_on_keeps_the_rewrite_it_had(self):
        bp = self.make({"f1-aok": "old"}, cut="")     # went to its unknown pile
        src = self.source("f1-aok", "f1-aok", "old")
        out = bp.refresh_rewrites(self.work(src))
        self.assertEqual((src.pick_name, src.head, self.planned), ("f1-aok", "old", []))
        self.assertEqual(out[0][1], ["master", "branch-3.5"])

    def test_picking_from_the_fork_does_not_re_cut_anything(self):
        bp = self.make({"f1-aok": "new"}, cut="f1-aok\n")
        bp.cfg.pick_from_fork = True
        src = self.source("f1-aok", "fork/f1", "forkhead")
        self.assertEqual(bp.refresh_rewrites(self.work(src)), self.work(src))
        self.assertEqual(self.ran, [])

    def test_a_dry_run_only_says_what_it_would_do(self):
        bp = self.make({"f1-aok": "new"}, cut="f1-aok\n")
        bp.cfg.dry_run = True
        src = self.source("f1-aok", "f1-aok", "old")
        self.assertEqual(bp.refresh_rewrites(self.work(src)), self.work(src))
        self.assertEqual((self.ran, src.head), ([], "old"))

    def test_turning_it_off_skips_both_helpers(self):
        bp = self.make({"f1-aok": "new"}, cut="f1-aok\n")
        bp.cfg.refresh_before_retry = False
        bp.refresh_rewrites(self.work(self.source("f1-aok", "f1-aok", "old")))
        self.assertEqual(self.ran, [])


class CiCoversTheChange(unittest.TestCase):
    """A rebase is not a difference; an empty rewrite is not a CI problem."""

    def make(self, signatures, empty=()):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        self.written = []
        bp.reports = SimpleNamespace(write=lambda name, *cols: self.written.append((name, cols)))
        bp.change_signature = lambda head: signatures.get(head)
        bp.carries_nothing = lambda head: head in empty
        return bp

    def source(self, head, fork_head):
        src = Source("f1-aok")
        src.pick_name, src.head = "f1-aok", head
        src.fork_name, src.fork_head = "f1", fork_head
        return src

    def test_the_same_sha_needs_no_comparing(self):
        bp = self.make({})
        src = self.source("same", "same")
        bp.warn_if_untested(src)
        self.assertTrue(src.ci_covers_change)
        self.assertEqual(self.written, [])

    def test_a_rebase_that_keeps_the_diff_is_covered_by_the_fork_s_ci(self):
        bp = self.make({"rewritten": "patch-1", "onfork": "patch-1"})
        src = self.source("rewritten", "onfork")
        bp.warn_if_untested(src)
        self.assertTrue(src.ci_covers_change)
        self.assertEqual(self.written, [])

    def test_a_different_diff_is_still_flagged(self):
        bp = self.make({"rewritten": "patch-2", "onfork": "patch-1"})
        src = self.source("rewritten", "onfork")
        bp.warn_if_untested(src)
        self.assertFalse(src.ci_covers_change)
        self.assertEqual([name for name, _ in self.written], ["NEEDS_REVIEW.txt"])

    def test_an_empty_rewrite_is_not_reported_as_a_ci_problem(self):
        # the rebase dropped every commit as already applied: nothing to cover
        bp = self.make({"onfork": "patch-1"}, empty=["rewritten"])
        src = self.source("rewritten", "onfork")
        bp.warn_if_untested(src)
        self.assertEqual(self.written, [])
        self.assertFalse(src.ci_covers_change)   # nothing to merge, nothing covered

    def test_a_signature_we_could_not_work_out_is_still_flagged(self):
        bp = self.make({"onfork": "patch-1"})     # picked is None, but not empty
        src = self.source("rewritten", "onfork")
        bp.warn_if_untested(src)
        self.assertEqual([name for name, _ in self.written], ["NEEDS_REVIEW.txt"])


class BuildInsteadOfWaiting(unittest.TestCase):
    """A round with nothing green builds the head of the queue rather than idle."""

    def make(self, states, stand_in=True, compile_=True):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(dry_run=False, fork_remote="fork", fork_repo="holdenk/spark",
                                 ci_poll_minutes=0, ci_wait_hours=0,
                                 local_stand_in=stand_in, do_compile=compile_)
        self.events = []
        bp.git = SimpleNamespace(run=lambda *a, **k: None)
        bp.reports = SimpleNamespace(write=lambda name, *cols: None)
        bp.record = lambda name, target, why: None
        bp.warn_if_untested = lambda src: None
        bp.merge_all = lambda work: self.events.extend(f"merge:{src.name}" for src, _ in work)
        bp.execute = lambda src, targets: self.events.append(f"build:{src.name}")
        bp.resolve_on_fork = lambda name: (name, name + "-head")
        bp.ci_status_of = lambda sha: states[sha]
        return bp

    def source(self, name):
        src = Source(name)
        src.fork_name, src.fork_head = name, name + "-head"
        return src

    def work(self, *names):
        return [(self.source(n), ["master"]) for n in names]

    def test_a_queued_branch_is_built_here_instead_of_waited_on(self):
        bp = self.make({"a-head": "running"})
        self.assertEqual(bp.revisit(self.work("a")), [])
        self.assertEqual(self.events, ["build:a"])

    def test_it_works_its_way_down_the_queue_one_at_a_time(self):
        bp = self.make({"a-head": "running", "b-head": "running", "c-head": "running"})
        bp.revisit(self.work("a", "b", "c"))
        self.assertEqual(self.events, ["build:a", "build:b", "build:c"])

    def test_a_branch_that_went_green_is_merged_before_anything_is_built(self):
        bp = self.make({"a-head": "passing", "b-head": "running"})
        bp.revisit(self.work("a", "b"))
        self.assertEqual(self.events, ["merge:a", "build:b"])

    def test_a_late_failure_is_still_handed_back_not_built(self):
        bp = self.make({"a-head": "failing", "b-head": "running"})
        late = bp.revisit(self.work("a", "b"))
        self.assertEqual([src.name for src, _ in late], ["a"])
        self.assertEqual(self.events, ["build:b"])

    def test_turning_it_off_leaves_the_old_polling_alone(self):
        bp = self.make({"a-head": "running", "b-head": "passing"}, stand_in=False)
        # b goes green on the first look, a would poll forever -- so stop it there
        bp.ci_status_of = lambda sha: {"a-head": "running", "b-head": "passing"}[sha]
        bp.cfg.ci_wait_hours = 1e-9        # deadline expires immediately
        bp.revisit(self.work("a", "b"))
        self.assertEqual(self.events, ["merge:b"])       # nothing built locally

    def test_with_no_local_build_there_is_nothing_to_stand_in_with(self):
        bp = self.make({"a-head": "running"}, compile_=False)
        bp.cfg.ci_wait_hours = 1e-9
        bp.revisit(self.work("a"))
        self.assertEqual(self.events, [])


class AlreadyLandedGroup(unittest.TestCase):
    """Work that is already upstream is tracked, not just skipped."""

    def test_a_branch_already_in_master_goes_to_already_landed(self):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(upstream="apache-github", branch_file=Path("b.txt"))
        bp.git = SimpleNamespace(subject=lambda rev: "[CORE] the work")
        written, recorded = [], []
        bp.reports = SimpleNamespace(write=lambda name, *cols: written.append((name, cols)))
        bp.record = lambda name, target, why: recorded.append(why)
        bp.pick_base = lambda src: "merged:master"

        src = Source("f003-r2-aok")
        src.head, src.fork_head = "abc", "def"
        self.assertIsNone(bp.plan(src))
        self.assertEqual([name for name, _ in written], ["already_landed.txt"])
        self.assertEqual(written[0][1][:4],
                         ("f003-r2-aok", "-", "abc", "[CORE] the work"))
        self.assertIn("master", written[0][1][4])
        self.assertEqual(recorded, ["skipped, already in master"])


class EmptyCherryPick(unittest.TestCase):
    """A pick that changes nothing is already-landed, not a conflict."""

    def make(self, results):
        """results: commit -> "ok" | "empty" | "conflict"."""
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(dry_run=False, fork_remote="fork", do_push=False,
                                 ci_shortcut=True, do_compile=False)
        self.written = []
        self.recorded = []
        self.state = "clean"          # what the last failed pick left behind
        self.aborted = []

        def ok(*args):
            if args[0] != "cherry-pick":
                return True
            outcome = results[args[-1]]
            self.state = "clean" if outcome == "empty" else "unmerged"
            return outcome == "ok"

        def out(*args):
            if args[:2] == ("rev-parse", "HEAD"):
                return "pre"
            if args[0] == "ls-files":
                return "both modified: f" if self.state == "unmerged" else ""
            if args[0] == "status":
                return ""
            return ""

        def run(*args, **kw):
            if args[:2] == ("cherry-pick", "--abort"):
                self.aborted.append(True)
            return SimpleNamespace(returncode=0)

        bp.git = SimpleNamespace(
            ok=ok, out=out, run=run, subject=lambda sha: f"subject of {sha}",
            has_ref=lambda ref: ref == "CHERRY_PICK_HEAD",
            lines=lambda *args: [] if args[0] == "cherry" else ["landed"],
        )
        bp.reports = SimpleNamespace(write=lambda name, *cols: self.written.append((name, cols)))
        bp.record = lambda name, target, why: self.recorded.append((target, why))
        bp.use_target = lambda target: True
        bp.sanity_check = lambda *a: True
        bp.ci_stands_in_for_build = lambda src, target: True
        bp.push_target = lambda target: True
        bp.find_landed = lambda created: list(created)
        bp.remember_merge = lambda src, target, landed: None
        return bp

    def source(self, commits):
        src = Source("f1-aok")
        src.head, src.base, src.commits = "head", "base", commits
        return src

    def files(self):
        return [name for name, _ in self.written]

    def test_an_empty_pick_is_tracked_instead_of_rejected(self):
        bp = self.make({"c1": "empty"})
        self.assertEqual(bp.backport(self.source(["c1"]), "branch-3.5"), [])
        self.assertEqual(self.files(), ["already_landed.txt", "skipped.txt"])
        self.assertNotIn("rejects.txt", self.files())
        self.assertEqual(self.aborted, [True])          # the stopped pick is undone

    def test_the_row_says_which_commit_on_which_target(self):
        bp = self.make({"c1": "empty"})
        bp.backport(self.source(["c1"]), "branch-3.5")
        name, cols = self.written[0]
        self.assertEqual(cols[:4], ("f1-aok", "branch-3.5", "c1", "subject of c1"))
        self.assertIn("empty", cols[4])

    def test_the_rest_of_the_series_carries_on(self):
        bp = self.make({"c1": "empty", "c2": "ok"})
        landed = bp.backport(self.source(["c1", "c2"]), "master")
        self.assertEqual(self.files(), ["already_landed.txt"])   # no skipped.txt: c2 went in
        self.assertEqual(landed, ["landed"])

    def test_a_real_conflict_is_still_a_conflict(self):
        bp = self.make({"c1": "conflict"})
        self.assertEqual(bp.backport(self.source(["c1"]), "branch-3.5"), [])
        self.assertEqual(self.files(), ["rejects.txt"])
        self.assertEqual(self.recorded, [("branch-3.5", "conflict, nothing applied")])

    def test_no_cherry_pick_in_progress_is_not_an_empty_pick(self):
        bp = self.make({"c1": "conflict"})
        bp.git.has_ref = lambda ref: False
        bp.backport(self.source(["c1"]), "branch-3.5")
        self.assertEqual(self.files(), ["rejects.txt"])


class BackporterAttributes(unittest.TestCase):
    """Every self.<name> the class reads must actually get set somewhere.

    Three separate bugs in this file were an attribute quietly dropped from
    __init__ by an edit; this catches that without running anything.
    """

    def test_no_attribute_is_read_before_it_is_set(self):
        import ast
        import merge_branches

        source = ast.parse(Path(merge_branches.__file__).read_text())
        cls = next(node for node in ast.walk(source)
                   if isinstance(node, ast.ClassDef) and node.name == "Backporter")
        defined = {item.name for item in cls.body
                   if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
        read = set()
        for node in ast.walk(cls):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "self"):
                if isinstance(node.ctx, ast.Store):
                    defined.add(node.attr)
                elif not node.attr.startswith("__"):
                    read.add(node.attr)
        self.assertEqual(read - defined, set(),
                         "read but never assigned in Backporter")


class RefSlug(unittest.TestCase):
    def test_slashes_and_spaces_become_underscores(self):
        self.assertEqual(ref_slug("feature/my branch--master"), "feature_my_branch--master")

    def test_safe_characters_survive(self):
        self.assertEqual(ref_slug("xss-fix-branch-3.5--branch-3.5"),
                         "xss-fix-branch-3.5--branch-3.5")


if __name__ == "__main__":
    unittest.main()
