#!/usr/bin/env python3
"""Unit tests for the pure rules in merge_branches.py.

python3 -m unittest test_merge_branches -v      (or: pytest test_merge_branches.py)
"""

import csv
import tempfile
import time
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
    is_sibling_cut,
    redo_revision,
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
        for branch in (
            "3.5-cve-fixes",
            "master-of-none",
            "fix-4.1-followup",
            "SPARK-59119-cyclic-cause_idx",
            "branch-4.10",
            "no-version-here",
        ):
            with self.subTest(branch=branch):
                self.assertIsNone(suffix_target(branch))

    def test_every_spelling_of_the_same_pin_agrees(self):
        """-4x, -4.x, with or without "-branch", with or without a revision."""
        stem = "f091-kafka-datasource-option-restrict"
        spellings = [
            f"{stem}{tail}"
            for tail in (
                "-4x",
                "-4.x",
                "-branch-4x",
                "-branch-4.x",
                "-4x-aok",
                "-branch-4x-squashed",
                "-branch-4.x-aok",
                "-4.x-r2",
                "-branch-4x-r2",
                "-branch-4.x-r2-aok",
            )
        ]
        for branch in spellings:
            with self.subTest(branch=branch):
                self.assertEqual(suffix_target(branch), "branch-4.x")
                self.assertEqual(family_stem(branch), stem)

    def test_a_revision_marker_does_not_hide_the_version(self):
        # SPARK-59118-doAs-warn-4.x-r2 is the second cut for 4.x, not an
        # unpinned branch that would go to every release.
        self.assertEqual(suffix_target("SPARK-59118-doAs-warn-4.x-r2"), "branch-4.x")
        self.assertEqual(family_stem("SPARK-59118-doAs-warn-4.x-r2"), "SPARK-59118-doAs-warn")

    def test_a_revision_marker_on_an_unpinned_name_is_part_of_the_name(self):
        # f003-r2 is the name of the work; there is no f003 to fold it into.
        self.assertIsNone(suffix_target("f003-r2-aok"))
        self.assertEqual(family_stem("f003-r2-aok"), "f003-r2")
        self.assertEqual(family_stem("f003-r2-branch-3.5-aok"), "f003-r2")
        self.assertEqual(suffix_target("f003-r2-branch-3.5-aok"), "branch-3.5")


class LogicalName(unittest.TestCase):
    def test_strips_the_squash_marker(self):
        self.assertEqual(
            logical_name("security-agg-injection-branch-4.2-aok"),
            "security-agg-injection-branch-4.2",
        )
        self.assertEqual(
            logical_name("ldap-thriftserver-improvement-squashed"), "ldap-thriftserver-improvement"
        )
        self.assertEqual(logical_name("some-fix-4.1-squashed"), "some-fix-4.1")

    def test_strips_stacked_markers(self):
        self.assertEqual(logical_name("thing-squashed-aok"), "thing")
        self.assertEqual(logical_name("thing-aok-squashed"), "thing")

    def test_leaves_an_unmarked_name_alone(self):
        self.assertEqual(
            logical_name("f107-improve-show-create-table-escaping"),
            "f107-improve-show-create-table-escaping",
        )

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
        self.assertEqual(suffix_target("security-agg-injection-branch-4.2-squashed"), "branch-4.2")
        self.assertEqual(suffix_target("udt-config-opt-squashed"), None)
        self.assertEqual(suffix_target("some-fix-master-aok"), "master")
        self.assertEqual(suffix_target("xss-fix-branch-4.x-aok"), "branch-4.x")

    def test_a_marked_branch_with_no_version_is_not_pinned(self):
        self.assertIsNone(suffix_target("f107-improve-show-create-table-escaping-aok"))
        self.assertIsNone(suffix_target("f007-mergedir-aok"))

    def test_routing_uses_the_stripped_name(self):
        self.assertEqual(
            targets_for("security-agg-injection-branch-4.2-aok", "master", ALL_TARGETS),
            ["branch-4.2"],
        )
        self.assertEqual(targets_for("f007-mergedir-aok", "master", ALL_TARGETS), ALL_TARGETS)


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

    def test_a_cancelled_job_does_not_pull_a_branch_out_of_the_wait(self):
        # GitHub cancels a superseded job as a matter of course -- a re-run on
        # the same sha, concurrency: cancel-in-progress -- while the jobs still
        # queued go on to pass
        self.assertEqual(ci_status([("completed", "cancelled"), ("queued", "")]),
                         "running")

    def test_a_gate_waiting_on_a_person_is_not_a_failure_either(self):
        self.assertEqual(ci_status([("completed", "action_required"), ("queued", "")]),
                         "running")

    def test_a_real_failure_still_does(self):
        for bad in ("failure", "timed_out", "startup_failure"):
            with self.subTest(bad=bad):
                self.assertEqual(ci_status([("completed", bad), ("queued", "")]),
                                 "running-with-failures")

    def test_a_cancelled_job_is_still_not_a_pass_once_everything_is_done(self):
        self.assertEqual(ci_status([("completed", "cancelled"),
                                    ("completed", "success")]), "failing")

    def test_running_wins_over_a_failure_but_says_so(self):
        # A branch still building is "come back later", not "rejected" -- but an
        # already-failed job should not be invisible while the rest queues.
        self.assertEqual(
            ci_status([("completed", "failure"), ("queued", "")]), "running-with-failures"
        )

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
        self.assertEqual(
            parse_branch_file("alpha\nbeta\nalpha\n"), [("alpha", None), ("beta", None)]
        )

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
        self.assertTrue(
            is_specific_subject(
                "[SPARK-59200][DOCS] Clarify the spark.eventLog.compress description"
            )
        )

    def test_a_generic_subject_is_not(self):
        for subject in ("[MINOR] Fix typo", "fix", "  Update docs  ", ""):
            with self.subTest(subject=subject):
                self.assertFalse(is_specific_subject(subject))


class Families(unittest.TestCase):
    """A change that will not apply everywhere arrives as a plain branch plus a
    per-release cut of the same work."""

    def test_stem_strips_markers_and_version(self):
        for name in (
            "f107-fix",
            "f107-fix-aok",
            "f107-fix-3.5",
            "f107-fix-3.5-aok",
            "f107-fix-4.1-squashed",
            "f107-fix-master",
            # the other spelling: -branch-3.5 rather than -3.5
            "f107-fix-branch-3.5",
            "f107-fix-branch-4.x-aok",
            "f107-fix-branch-master",
        ):
            with self.subTest(name=name):
                self.assertEqual(family_stem(name), "f107-fix")

    def test_mixed_spellings_pair_with_the_plain_branch(self):
        listed = [
            "f091-kafka-datasource-option-restrict",
            "f091-kafka-datasource-option-restrict-4x",
            "f091-kafka-datasource-option-restrict-branch-3.5-aok",
        ]
        self.assertEqual(
            sibling_coverage("f091-kafka-datasource-option-restrict", ALL_TARGETS, listed),
            {
                "branch-4.x": "f091-kafka-datasource-option-restrict-4x",
                "branch-3.5": "f091-kafka-datasource-option-restrict-branch-3.5-aok",
            },
        )

    def test_a_name_that_really_ends_in_branch_keeps_it(self):
        # only the "-branch" that came in front of a version is dropped
        self.assertEqual(
            family_stem("security-agg-injection-branch"), "security-agg-injection-branch"
        )

    def test_the_branch_spelling_pairs_with_the_plain_one(self):
        listed = [
            "f040-compressed-file-handling",
            "f040-compressed-file-handling-branch-3.5",
            "f040-compressed-file-handling-branch-4.1",
        ]
        self.assertEqual(
            sibling_coverage("f040-compressed-file-handling", ALL_TARGETS, listed),
            {
                "branch-3.5": "f040-compressed-file-handling-branch-3.5",
                "branch-4.1": "f040-compressed-file-handling-branch-4.1",
            },
        )

    def test_the_plain_branch_leaves_35_to_its_sibling(self):
        listed = ["f107-fix-aok", "f107-fix-3.5-aok"]
        self.assertEqual(
            sibling_coverage("f107-fix-aok", ALL_TARGETS, listed),
            {"branch-3.5": "f107-fix-3.5-aok"},
        )

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
            {"branch-3.5": "thing-3.5-aok", "branch-4.1": "thing-4.1-aok"},
        )

    def test_a_sibling_for_a_target_not_in_play_is_ignored(self):
        listed = ["thing-aok", "thing-4.3-aok"]
        self.assertEqual(sibling_coverage("thing-aok", ALL_TARGETS, listed), {})


class UnlistedSiblings(unittest.TestCase):
    """The pair that arrives when a change will not apply to every release."""

    def make(self, listed, on_fork, targets=("master", "branch-4.1", "branch-3.5"),
             local=(), base="master", held=()):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(strip_suffixes=["-aok", "-squashed"],
                                 fork_remote="origin", targets=list(targets),
                                 branch_file="branches_to_merge.txt",
                                 hold_list="HELD_BRANCHES.txt",
                                 adopt_siblings=True)
        bp.listed = list(listed)
        bp.held = {name: "listed" for name in held}
        # the base the listed branch was cut from decides where it goes, and so
        # decides which of its siblings are wanted
        bp.pick_base = lambda src: ("abc123", base) if base else None
        bp._branch_names = (list(on_fork), list(local))
        refs = {f"refs/remotes/origin/{name}" for name in on_fork}
        refs |= {f"refs/heads/{name}" for name in local}
        bp.git = SimpleNamespace(
            has_ref=lambda ref: ref in refs,
            rev_parse=lambda ref: "forkhead" if ref.rstrip("^{commit}") in refs
            or ref.split("^")[0] in refs else None)
        return bp

    def test_a_pinned_branch_has_no_siblings_to_look_for(self):
        # it owns that one release; every name generated for it here is just
        # another spelling of itself, which is what used to be reported
        name = "22-zk-recovery-branch-4.0-aok"
        bp = self.make([name], ["22-zk-recovery-branch-4.0"], targets=["branch-4.0"])
        self.assertIsNone(bp.unlisted_sibling(Source(name), "branch-4.0"))
        self.assertEqual(bp.adopt_siblings([Source(name)]), [])

    def test_a_sibling_listed_under_a_different_marker_is_not_missing(self):
        # the branch file has the local rewrite, the fork has the plain name
        bp = self.make(["myfix-aok", "myfix-4.1-aok"], ["myfix", "myfix-4.1"])
        self.assertIsNone(bp.unlisted_sibling(Source("myfix-aok"), "branch-4.1"))

    def test_a_cut_that_really_is_missing_is_found(self):
        bp = self.make(["myfix-aok"], ["myfix", "myfix-branch-3.5"])
        self.assertEqual(bp.unlisted_sibling(Source("myfix-aok"), "branch-3.5"),
                         "myfix-branch-3.5")

    def test_a_missing_cut_is_taken_in(self):
        bp = self.make(["myfix-aok"], ["myfix", "myfix-branch-3.5"])
        self.assertEqual([src.name for src in bp.adopt_siblings([Source("myfix-aok")])],
                         ["myfix-branch-3.5"])

    def test_it_is_taken_in_under_the_local_rewrite_name(self):
        # squash-magic leaves the rewrite as -aok and never touches the original,
        # so adopting the fork spelling would make resolve_pick_ref() pick from
        # that untouched, un-squashed original
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1"], local=["myfix-4.1-aok"])
        self.assertEqual([src.name for src in bp.adopt_siblings([Source("myfix-aok")])],
                         ["myfix-4.1-aok"])

    def test_the_ref_it_matched_is_carried_with_it(self):
        # the rewrite can be spelled quite differently from the fork's copy, and
        # resolve_on_fork() would go looking for a ref that is not there
        bp = self.make(["myfix-aok"], ["myfix", "myfix-branch-4.1"],
                       local=["myfix-4.1-aok"])
        taken = bp.adopt_siblings([Source("myfix-aok")])
        self.assertEqual([(src.name, src.fork_name) for src in taken],
                         [("myfix-4.1-aok", "myfix-branch-4.1")])

    def test_a_superseded_rewrite_is_not_taken_for_the_redo(self):
        # the fork has r2 and we pick it, but cut_identity drops the -rN, so the
        # rewrite of the superseded r1 answers to the same identity.  Taking it
        # would merge r1's commits under r2's green tick.
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1", "myfix-4.1-r2"],
                       local=["myfix-4.1-aok"])
        taken = bp.adopt_siblings([Source("myfix-aok")])
        self.assertEqual([(src.name, src.fork_name) for src in taken],
                         [("myfix-4.1-r2", "myfix-4.1-r2")])

    def test_the_rewrite_of_the_right_revision_is_taken(self):
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1-r2"],
                       local=["myfix-4.1-aok", "myfix-4.1-r2-aok"])
        taken = bp.adopt_siblings([Source("myfix-aok")])
        self.assertEqual([src.name for src in taken], ["myfix-4.1-r2-aok"])

    def test_the_rewrite_is_found_under_a_different_spelling(self):
        # the fork carries myfix-branch-4.1, squash-magic left myfix-4.1-aok:
        # the same cut, and sticking "-aok" on the fork name finds neither
        bp = self.make(["myfix-aok"], ["myfix", "myfix-branch-4.1"],
                       local=["myfix-4.1-aok"])
        self.assertEqual([src.name for src in bp.adopt_siblings([Source("myfix-aok")])],
                         ["myfix-4.1-aok"])

    def test_the_untouched_original_is_not_mistaken_for_a_rewrite(self):
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1"], local=["myfix-4.1"])
        self.assertEqual([src.name for src in bp.adopt_siblings([Source("myfix-aok")])],
                         ["myfix-4.1"])

    def test_without_a_local_rewrite_the_fork_name_is_used(self):
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1"])
        self.assertEqual([src.name for src in bp.adopt_siblings([Source("myfix-aok")])],
                         ["myfix-4.1"])

    def test_it_is_taken_in_once_however_many_branches_point_at_it(self):
        # two cuts of the same work in the list, one sibling between them
        bp = self.make(["myfix-aok", "myfix-squashed"], ["myfix", "myfix-branch-3.5"])
        taken = bp.adopt_siblings([Source("myfix-aok"), Source("myfix-squashed")])
        self.assertEqual([src.name for src in taken], ["myfix-branch-3.5"])

    def test_a_branch_of_unrelated_work_is_not_swept_up(self):
        bp = self.make(["myfix-aok"], ["myfix", "somethingelse-branch-3.5"])
        self.assertEqual(bp.adopt_siblings([Source("myfix-aok")]), [])

    def test_a_cut_for_a_release_the_parent_never_goes_to_is_left_alone(self):
        # cut from branch-3.5, so it is 3.5-only; a stale myfix-4.1 on the fork
        # is somebody's abandoned attempt, not the cut branch-4.1 needs, and the
        # listed branch was never going to try branch-4.1 anyway
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1"], base="branch-3.5")
        self.assertEqual(bp.adopt_siblings([Source("myfix-aok")]), [])

    def test_a_cut_for_a_release_the_parent_does_go_to_is_taken(self):
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1"], base="master")
        self.assertEqual([src.name for src in bp.adopt_siblings([Source("myfix-aok")])],
                         ["myfix-4.1"])

    def test_nothing_is_adopted_when_the_base_cannot_be_worked_out(self):
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1"], base=None)
        self.assertEqual(bp.adopt_siblings([Source("myfix-aok")]), [])

    def test_a_held_branch_does_not_bring_its_family_in(self):
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1"], held=["myfix-aok"])
        self.assertEqual(bp.adopt_siblings([Source("myfix-aok")]), [])

    def test_a_held_cut_is_not_adopted(self):
        # you held it because you know it is wrong; adopting it would also take
        # branch-4.1 off the listed branch, so the held cut would be the only
        # thing landing there
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1"], held=["myfix-4.1"])
        self.assertEqual(bp.adopt_siblings([Source("myfix-aok")]), [])

    def test_a_redone_cut_on_the_fork_is_found(self):
        # -r2 is understood everywhere else; a generator that never emitted it
        # made the redone cut invisible to the one check meant to find it
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1-r2"])
        self.assertEqual(bp.unlisted_sibling(Source("myfix-aok"), "branch-4.1"),
                         "myfix-4.1-r2")

    def test_the_newest_redo_wins_when_both_are_lying_about(self):
        # -r2 exists because -r1 was superseded, and both usually survive
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1", "myfix-4.1-r2"])
        self.assertEqual(bp.unlisted_sibling(Source("myfix-aok"), "branch-4.1"),
                         "myfix-4.1-r2")

    def test_a_redone_cut_still_counts_as_listed(self):
        # -r2 marks a cut that was redone, so the first one is exactly the stale
        # ref lying around -- and the redo is the branch you listed
        bp = self.make(["myfix-aok", "myfix-4.1-r2-aok"], ["myfix", "myfix-4.1"])
        self.assertIsNone(bp.unlisted_sibling(Source("myfix-aok"), "branch-4.1"))
        self.assertEqual(bp.adopt_siblings([Source("myfix-aok")]), [])

    def test_a_held_parent_holds_its_family_under_any_spelling(self):
        # the branch file lists myfix-aok, you held the fork's myfix: one branch
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1"], held=["myfix"])
        self.assertEqual(bp.adopt_siblings([Source("myfix-aok")]), [])

    def test_holding_a_parent_does_not_hold_a_different_release(self):
        # myfix and myfix-4.1 are different cuts, not spellings of one
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1"], held=["myfix"])
        self.assertIsNone(bp.is_held("myfix-4.1"))

    def test_a_held_cut_is_recognised_under_any_spelling(self):
        # you hold the name squash-magic gave you, the fork carries another
        bp = self.make(["myfix-aok"], ["myfix", "myfix-4.1"], held=["myfix-4.1-aok"])
        self.assertEqual(bp.adopt_siblings([Source("myfix-aok")]), [])

    def test_the_fork_copy_wins_over_a_stale_local_ref(self):
        # a local myfix-4.1 is generated first, but only a fork ref can be
        # adopted -- returning the local one hides the cut that is really there
        bp = self.make(["myfix-aok"], ["myfix", "myfix-branch-4.1"],
                       local=["myfix-4.1"])
        self.assertEqual(bp.unlisted_sibling(Source("myfix-aok"), "branch-4.1"),
                         "myfix-branch-4.1")
        self.assertEqual([src.name for src in bp.adopt_siblings([Source("myfix-aok")])],
                         ["myfix-branch-4.1"])

    def test_a_listed_cut_wins_over_a_stale_ref_spelled_differently(self):
        # myfix-4.1 is generated before myfix-branch-4.1, so a stale ref under
        # the first spelling must not beat the cut you actually listed
        bp = self.make(["myfix-aok", "myfix-branch-4.1-aok"],
                       ["myfix", "myfix-branch-4.1", "myfix-4.1"])
        self.assertIsNone(bp.unlisted_sibling(Source("myfix-aok"), "branch-4.1"))
        self.assertEqual(bp.adopt_siblings([Source("myfix-aok")]), [])

    def test_a_local_only_cut_is_reported_rather_than_adopted(self):
        # adopting it would take branch-4.1 off the listed branch and then drop
        # the sibling for having no CI, leaving nobody on that release
        bp = self.make(["myfix-aok"], ["myfix"], local=["myfix-4.1"])
        self.assertEqual(bp.adopt_siblings([Source("myfix-aok")]), [])
        self.assertEqual(bp.unlisted_sibling(Source("myfix-aok"), "branch-4.1"),
                         "myfix-4.1")          # still worth warning about


class AdoptionIsOptIn(unittest.TestCase):
    def test_it_is_off_unless_asked_for(self):
        import merge_branches
        self.assertFalse(merge_branches.parse_args([]).adopt_siblings)
        self.assertTrue(merge_branches.parse_args(["--adopt-siblings"]).adopt_siblings)


class IsSiblingCut(unittest.TestCase):
    """Which existing branch names are a cut of this work for this release."""

    def test_every_spelling_of_the_same_cut_counts(self):
        for name in ("myfix-3.5", "myfix-3.5-aok", "myfix-3.5-squashed",
                     "myfix-branch-3.5", "myfix-branch-3.5-aok", "myfix-3.5-r2",
                     "myfix-branch-3.5-r7-squashed"):
            with self.subTest(name=name):
                self.assertTrue(is_sibling_cut(name, "myfix", "branch-3.5"))

    def test_both_spellings_of_4x_count(self):
        for name in ("myfix-4x", "myfix-4.x", "myfix-branch-4.x"):
            with self.subTest(name=name):
                self.assertTrue(is_sibling_cut(name, "myfix", "branch-4.x"))

    def test_a_cut_for_another_release_does_not(self):
        self.assertFalse(is_sibling_cut("myfix-4.1", "myfix", "branch-3.5"))

    def test_unrelated_work_does_not(self):
        self.assertFalse(is_sibling_cut("somethingelse-3.5", "myfix", "branch-3.5"))

    def test_a_pinned_branch_has_no_siblings(self):
        self.assertFalse(is_sibling_cut("myfix-3.5", "myfix-4.1", "branch-3.5"))

    def test_the_branch_is_never_its_own_sibling(self):
        # a --strip-suffix that swallows a version makes the branch read as
        # unpinned, and it would then match as a cut of itself
        self.assertFalse(is_sibling_cut("myfix-3.5", "myfix-3.5", "branch-3.5", ["-3.5"]))


class RedoRevision(unittest.TestCase):
    def test_a_plain_cut_is_revision_zero(self):
        self.assertEqual(redo_revision("myfix-4.1"), 0)

    def test_a_redo_carries_its_number(self):
        self.assertEqual(redo_revision("myfix-4.1-r2"), 2)
        self.assertEqual(redo_revision("myfix-branch-4.1-r10-aok"), 10)

    def test_a_name_that_merely_ends_in_r_is_not_a_redo(self):
        self.assertEqual(redo_revision("myfix-refactor"), 0)


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
        path = self.ledger(
            [
                ["2026-08-31T10:00:00-07:00", "f003-r2-aok", "master", "abc", "def", "apache"],
                ["2026-08-31T10:05:00-07:00", "f003-r2-aok", "branch-4.1", "ghi", "def", "apache"],
            ]
        )
        self.assertEqual(
            load_ledger(path), {("f003-r2-aok", "master"), ("f003-r2-aok", "branch-4.1")}
        )

    def test_blank_and_ragged_rows_are_ignored(self):
        path = self.ledger(
            [
                ["2026-08-31T10:00:00-07:00", "good-aok", "master", "abc", "def", "apache"],
                ["2026-08-31T10:00:00-07:00", "", "master", "", "", ""],
                ["2026-08-31T10:00:00-07:00", "no-target", "", "", "", ""],
            ]
        )
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
        path = self.hold_file("# branches on hold\nfoo-aok\tflaky test, chasing it\nbar-aok\n")
        held = load_hold_list(path)
        self.assertEqual(set(held), {"foo-aok", "bar-aok"})
        self.assertEqual(held["foo-aok"], "flaky test, chasing it")
        self.assertEqual(held["bar-aok"], "listed")

    def test_a_hold_with_no_reason_is_still_a_hold(self):
        # callers ask "is this held"; an empty reason read back as "no", and the
        # branch was located, approved and pushed
        for body in ("myfix\t\n", "myfix\n", "myfix\t   \n"):
            with self.subTest(body=body):
                self.assertEqual(load_hold_list(self.hold_file(body)),
                                 {"myfix": "listed"})

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
        path = self.approvals(
            [
                ["2026-09-01T01:00:00-07:00", "foo-aok", "abc", "patch111", "master"],
                ["2026-09-01T01:00:01-07:00", "bar-aok", "def", "patch222", "master branch-4.1"],
            ]
        )
        self.assertEqual(load_approvals(path), {("foo-aok", "patch111"), ("bar-aok", "patch222")})

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
                                 ci_wait_hours=0, ci_standin_tasks=["test"],
                                 ci_standin_timeout=21600, refresh_before_retry=False)
        bp.git = SimpleNamespace(run=lambda *a, **k: None)
        self.written = []
        self.recorded = []
        self.merged = []
        bp.reports = SimpleNamespace(write=lambda name, *cols: self.written.append((name, cols)))
        bp._checkout_failed = set()
        bp.record = lambda name, target, why: self.recorded.append((name, why))
        bp.warn_if_untested = lambda src: None
        bp.merge_all = lambda work: self.merged.extend(
            name for (src, _) in work for name in [src.name]
        )
        bp.ci_status_of = lambda sha: states[sha]
        heads = heads or {}
        bp.resolve_on_fork = lambda name: heads.get(name, (name, name + "-head"))
        bp.git.rev_parse = lambda ref: heads.get(
            ref.rsplit("/", 1)[-1].split("^")[0],
            (None, ref.rsplit("/", 1)[-1].split("^")[0] + "-head"))[1]
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

    def test_a_red_job_on_the_second_look_is_also_written_off(self):
        # it may still have jobs going, but one has finished red on this commit
        # both times we looked -- "come back later" would hand it another retry
        # it has already spent
        bp = self.make({"a-head": "running-with-failures"})
        bp.retry_failed([(self.source("a"), ["master"])])
        self.assertEqual(self.merged, [])
        self.assertEqual([name for name, _ in self.written], ["FAILING_CI.txt"])

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
        bp.refresh_rewrites = lambda work: seen.append([src.name for src, _ in work]) or work
        bp.retry_failed([(self.source("a"), ["master"])])
        self.assertEqual(seen, [["a"]])  # and before anything was merged
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
        self.assertEqual(self.written, [])  # retry_failed decides, not revisit


class SquashOutput(unittest.TestCase):
    def test_the_rewrites_are_found_by_the_name_they_share(self):
        made = squash_output("f1-aok\nf2-squashed\n\n# a comment\n")
        self.assertEqual(made, {"f1": "f1-aok", "f2": "f2-squashed"})

    def test_a_branch_that_came_back_under_a_different_marker_still_matches(self):
        # listed as f1-aok, but a second commit turned up and it is -squashed now
        self.assertEqual(squash_output("f1-squashed\n").get(logical_name("f1-aok")), "f1-squashed")


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
            refresh_before_retry=True,
            pick_from_fork=False,
            dry_run=False,
            strip_suffixes=merge_branches.STRIP_SUFFIXES,
            upstream="up",
            fork_remote="fork",
            log_dir=Path(self.tmp.name),
            update_bases=Path("update-bases.sh"),
            squash_magic=Path("squash-magic.sh"),
            refresh_timeout=60,
        )
        bp.git = SimpleNamespace(
            rev_parse=lambda ref: heads.get(
                ref.removeprefix("refs/heads/").removesuffix("^{commit}")
            )
        )
        self.ran = []
        self.planned = []

        def run_helper(script, args, env):
            self.ran.append((script.name, list(args)))
            if script.name == "squash-magic.sh":
                (Path(self.tmp.name) / "stamp-refresh-branches.txt").write_text(cut)
            return helpers

        bp.run_helper = run_helper
        bp.plan = lambda src: self.planned.append(src.name) or ["master"]
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
        self.assertEqual([name for name, _ in self.ran], ["update-bases.sh", "squash-magic.sh"])
        self.assertEqual(out[0][1], ["master", "branch-3.5"])  # not planned again
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
        self.assertEqual((src.name, src.pick_name, src.head), ("f1-aok", "f1-squashed", "new"))

    def test_nothing_left_to_merge_drops_the_branch(self):
        bp = self.make({"f1-aok": "new"}, cut="f1-aok\n")
        bp.plan = lambda src: None
        self.assertEqual(bp.refresh_rewrites(self.work(self.source("f1-aok", "f1-aok", "old"))), [])

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
        bp = self.make({"f1-aok": "old"}, cut="")  # went to its unknown pile
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
        self.assertFalse(src.ci_covers_change)  # nothing to merge, nothing covered

    def test_a_signature_we_could_not_work_out_is_still_flagged(self):
        bp = self.make({"onfork": "patch-1"})  # picked is None, but not empty
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
                                 local_stand_in=stand_in, do_compile=compile_,
                                 ci_standin_tasks=["test"],
                                 ci_standin_timeout=21600)
        self.events = []
        bp.git = SimpleNamespace(run=lambda *a, **k: None, ok=lambda *a: True,
                                 why=lambda: "")
        bp.reports = SimpleNamespace(write=lambda name, *cols: None)
        bp._checkout_failed = set()
        bp.record = lambda name, target, why: None
        bp.warn_if_untested = lambda src: None
        bp.merge_all = lambda work: self.events.extend(f"merge:{src.name}" for src, _ in work)
        bp.execute = lambda src, targets: self.events.append(f"build:{src.name}")
        bp.stand_in_for_ci = lambda src: None
        bp.resolve_on_fork = lambda name: (name, name + "-head")
        bp.git.rev_parse = lambda ref: ref.rsplit("/", 1)[-1].split("^")[0] + "-head"
        bp.ci_status_of = lambda sha: states[sha]
        bp._gh_complained = set()
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
        self.assertEqual(self.events, ["build:a", "build:b",
                                       "build:c"])

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
        bp.cfg.ci_wait_hours = 1e-9  # deadline expires immediately
        bp.revisit(self.work("a", "b"))
        self.assertEqual(self.events, ["merge:b"])  # nothing built locally

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
        bp._checkout_failed = set()
        bp.record = lambda name, target, why: recorded.append(why)
        bp.pick_base = lambda src: "merged:master"

        src = Source("f003-r2-aok")
        src.head, src.fork_head = "abc", "def"
        self.assertIsNone(bp.plan(src))
        self.assertEqual([name for name, _ in written], ["already_landed.txt"])
        self.assertEqual(written[0][1][:4], ("f003-r2-aok", "-", "abc", "[CORE] the work"))
        self.assertIn("master", written[0][1][4])
        self.assertEqual(recorded, ["skipped, already in master"])


class EmptyCherryPick(unittest.TestCase):
    """A pick that changes nothing is already-landed, not a conflict."""

    def make(self, results):
        """results: commit -> "ok" | "empty" | "conflict"."""
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(
            dry_run=False, fork_remote="fork", do_push=False, ci_shortcut=True, do_compile=False
        )
        self.written = []
        self.recorded = []
        self.state = "clean"  # what the last failed pick left behind
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
            ok=ok,
            out=out,
            run=run,
            subject=lambda sha: f"subject of {sha}",
            has_ref=lambda ref: ref == "CHERRY_PICK_HEAD",
            lines=lambda *args: [] if args[0] == "cherry" else ["landed"],
        )
        bp.reports = SimpleNamespace(write=lambda name, *cols: self.written.append((name, cols)))
        bp._checkout_failed = set()
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
        self.assertEqual(self.aborted, [True])  # the stopped pick is undone

    def test_the_row_says_which_commit_on_which_target(self):
        bp = self.make({"c1": "empty"})
        bp.backport(self.source(["c1"]), "branch-3.5")
        name, cols = self.written[0]
        self.assertEqual(cols[:4], ("f1-aok", "branch-3.5", "c1", "subject of c1"))
        self.assertIn("empty", cols[4])

    def test_the_rest_of_the_series_carries_on(self):
        bp = self.make({"c1": "empty", "c2": "ok"})
        landed = bp.backport(self.source(["c1", "c2"]), "master")
        self.assertEqual(self.files(), ["already_landed.txt"])  # no skipped.txt: c2 went in
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
        cls = next(
            node
            for node in ast.walk(source)
            if isinstance(node, ast.ClassDef) and node.name == "Backporter"
        )
        defined = {
            item.name
            for item in cls.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        read = set()
        for node in ast.walk(cls):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                if isinstance(node.ctx, ast.Store):
                    defined.add(node.attr)
                elif not node.attr.startswith("__"):
                    read.add(node.attr)
        self.assertEqual(read - defined, set(), "read but never assigned in Backporter")


class WaitingForCi(unittest.TestCase):
    """revisit() polls until CI settles; a blip is not a settlement."""

    def make(self, answers):
        """`answers` maps a sha to the states it returns, one per look."""
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(dry_run=False, fork_remote="fork",
                                 fork_repo="holdenk/spark", ci_poll_minutes=0,
                                 ci_wait_hours=0, local_stand_in=False,
                                 do_compile=False, ci_standin_tasks=["test"],
                                 ci_standin_timeout=21600)
        bp.git = SimpleNamespace(run=lambda *a, **k: None)
        self.written = []
        self.merged = []
        bp.reports = SimpleNamespace(write=lambda name, *cols: self.written.append(name))
        bp._checkout_failed = set()
        bp.record = lambda *a: None
        bp.warn_if_untested = lambda src: None
        bp.merge_all = lambda work: self.merged.extend(src.name for src, _ in work)
        queues = {sha: list(states) for sha, states in answers.items()}
        bp.ci_status_of = lambda sha: queues[sha].pop(0)
        bp.resolve_on_fork = lambda name: (name, name + "-head")
        bp.git.rev_parse = lambda ref: ref.rsplit("/", 1)[-1].split("^")[0] + "-head"
        bp._gh_complained = set()
        return bp

    def source(self, name):
        src = Source(name)
        src.fork_name, src.fork_head = name, name + "-head"
        return src

    def test_a_blip_does_not_end_a_wait(self):
        # GitHub between workflow runs reads as "no runs at all"; the branch was
        # building a moment ago, so ask again rather than write it off.
        bp = self.make({"a-head": ["none", "running", "passing"]})
        bp.revisit([(self.source("a"), ["master"])])
        self.assertEqual((self.merged, self.written), (["a"], []))

    def test_a_blip_that_never_clears_is_given_up_on(self):
        import merge_branches
        bp = self.make({"a-head": ["none"] * merge_branches.CI_MISREADS_ALLOWED})
        bp.revisit([(self.source("a"), ["master"])])
        self.assertEqual((self.merged, self.written), ([], ["TO_MERGE_LATER.txt"]))

    def test_a_usable_answer_in_between_resets_the_count(self):
        import merge_branches
        answers = (["none"] * (merge_branches.CI_MISREADS_ALLOWED - 1)
                   + ["running"]
                   + ["none"] * (merge_branches.CI_MISREADS_ALLOWED - 1)
                   + ["passing"])
        bp = self.make({"a-head": answers})
        bp.revisit([(self.source("a"), ["master"])])
        self.assertEqual((self.merged, self.written), (["a"], []))

    def test_a_misread_branch_is_not_built_in_place_of_its_ci(self):
        # the local build stands in for CI that is queued, not for CI that is
        # not there at all -- a branch we are only re-asking about has no tick
        # to stand in for
        import merge_branches
        bp = self.make({"a-head": ["none"] * merge_branches.CI_MISREADS_ALLOWED})
        bp.cfg.local_stand_in = True
        bp.cfg.do_compile = True
        nudged = []
        bp.build_instead_of_waiting = lambda queue, deadline=None: nudged.append(
            [src.name for src, _ in queue]) or None
        bp.revisit([(self.source("a"), ["master"])])
        self.assertTrue(all(offered == [] for offered in nudged), nudged)
        self.assertEqual((self.merged, self.written), ([], ["TO_MERGE_LATER.txt"]))

    def test_a_branch_with_a_failed_job_leaves_the_poll(self):
        # "running-with-failures" means GitHub has already failed a job on this
        # commit, and the jobs still going cannot unfail it.  Polling it waits
        # out the whole --ci-wait for a state that never resolves; a local build
        # does not answer it either.  It goes to the end-of-run retry, where a
        # re-run of the red job is what would be picked up.
        bp = self.make({"a-head": ["running-with-failures"]})
        bp.cfg.local_stand_in = True
        bp.cfg.do_compile = True
        nudged = []
        bp.build_instead_of_waiting = lambda queue, deadline=None: nudged.append(
            [src.name for src, _ in queue]) or None
        late = bp.revisit([(self.source("a"), ["master"])])
        self.assertEqual(nudged, [])              # never offered to a local build
        self.assertEqual(self.merged, [])
        self.assertEqual([src.name for src, _ in late], ["a"])   # kept for the retry

    def test_a_branch_that_really_is_building_can_be_stood_in_for(self):
        # ...but only once the queue has been quiet long enough to be worth hours
        import merge_branches
        quiet = merge_branches.IDLE_ROUNDS_BEFORE_STANDING_IN
        bp = self.make({"a-head": ["running"] * quiet + ["passing"]})
        bp.cfg.local_stand_in = True
        bp.cfg.do_compile = True
        nudged = []
        bp.build_instead_of_waiting = lambda queue, deadline=None: nudged.append(
            [src.name for src, _ in queue]) or None
        bp.revisit([(self.source("a"), ["master"])])
        self.assertEqual(nudged, [["a"]])
        self.assertEqual(self.merged, ["a"])

    def test_one_quiet_round_is_not_enough_to_buy_hours(self):
        # ten branches behind a busy runner pool are all "running" on round one
        # and may all go green a minute later; committing to a multi-hour test
        # run then means noticing none of them for the length of it
        import merge_branches
        quiet = merge_branches.IDLE_ROUNDS_BEFORE_STANDING_IN
        bp = self.make({"a-head": ["running"] * (quiet - 1) + ["passing"]})
        bp.cfg.local_stand_in = True
        bp.cfg.do_compile = True
        nudged = []
        bp.build_instead_of_waiting = lambda queue, deadline=None: nudged.append(
            [src.name for src, _ in queue]) or None
        bp.revisit([(self.source("a"), ["master"])])
        self.assertEqual(nudged, [])              # never asked
        self.assertEqual(self.merged, ["a"])      # and it merged on its own tick

    def test_the_failing_endpoint_is_named_once(self):
        # "could not read CI" is the same line whichever half broke, and the fix
        # for one of them is a token scope that nothing in the log points at
        import merge_branches
        from types import SimpleNamespace
        from unittest import mock

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(fork_repo="holdenk/spark")
        bp._gh_complained = set()
        bp._gh_lines = lambda path, jq: [] if "check-runs" in path else None
        said = []
        with mock.patch.object(merge_branches, "warn", said.append):
            for _ in range(5):
                bp.ci_status_of("deadbeef")
        self.assertEqual(len(said), 1)                 # said once, not per branch
        self.assertIn("commit statuses", said[0])
        self.assertIn("Commit statuses: read", said[0])

    def test_an_unreadable_legacy_status_is_an_error_not_an_absence(self):
        # check-runs can legitimately be empty -- some jobs only report through
        # the older commit-status API -- so a failure of that second call must
        # not come back as "this branch has no CI"
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(fork_repo="holdenk/spark")
        bp._gh_complained = set()
        bp._gh_lines = lambda path, jq: [] if "check-runs" in path else None
        self.assertEqual(bp.ci_status_of("deadbeef"), "error")

    def test_a_conclusive_failure_survives_an_unreadable_legacy_status(self):
        # nothing in the legacy statuses can turn a red check-run green, so
        # there is no reason to throw the answer away and poll again
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(fork_repo="holdenk/spark")
        bp._gh_complained = set()
        red = '{"status": "completed", "conclusion": "failure"}'
        bp._gh_lines = lambda path, jq: [red] if "check-runs" in path else None
        self.assertEqual(bp.ci_status_of("deadbeef"), "failing")

    def test_a_still_running_check_run_does_not_survive_one_either(self):
        # "running" is what makes a branch eligible for the local stand-in, and
        # the stand-in pushes -- so a half-read state must not reach it
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(fork_repo="holdenk/spark")
        bp._gh_complained = set()
        queued = '{"status": "queued", "conclusion": null}'
        bp._gh_lines = lambda path, jq: [queued] if "check-runs" in path else None
        self.assertEqual(bp.ci_status_of("deadbeef"), "error")

    def test_a_red_job_among_running_ones_survives_a_half_read_too(self):
        # as conclusive as "failing" by TERMINAL_FAILURES' own definition, and
        # returning "error" would drop the branch instead of giving it the retry
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(fork_repo="holdenk/spark")
        bp._gh_complained = set()
        runs = ['{"status": "completed", "conclusion": "failure"}',
                '{"status": "queued", "conclusion": null}']
        bp._gh_lines = lambda path, jq: runs if "check-runs" in path else None
        self.assertEqual(bp.ci_status_of("deadbeef"), "running-with-failures")

    def test_a_green_check_run_does_not_survive_one(self):
        # it might have: a red legacy status would be invisible in the half we
        # did read, and this is the verdict that pushes
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(fork_repo="holdenk/spark")
        bp._gh_complained = set()
        green = '{"status": "completed", "conclusion": "success"}'
        bp._gh_lines = lambda path, jq: [green] if "check-runs" in path else None
        self.assertEqual(bp.ci_status_of("deadbeef"), "error")

    def test_an_empty_pair_of_answers_really_is_no_ci(self):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(fork_repo="holdenk/spark")
        bp._gh_complained = set()
        bp._gh_lines = lambda path, jq: []
        self.assertEqual(bp.ci_status_of("deadbeef"), "none")

    def test_an_api_error_is_asked_again_before_it_is_believed(self):
        import merge_branches
        from types import SimpleNamespace
        from unittest import mock

        bp = object.__new__(merge_branches.Backporter)
        answers = ["error", "error", "passing"]
        bp.ci_status_of = lambda sha: answers.pop(0)
        with mock.patch.object(merge_branches.time, "sleep"):
            self.assertEqual(bp.ci_status_settled("deadbeef"), "passing")
        self.assertEqual(answers, [])

    def test_an_error_that_never_clears_is_still_an_error(self):
        import merge_branches
        from unittest import mock

        bp = object.__new__(merge_branches.Backporter)
        looks = []
        bp.ci_status_of = lambda sha: looks.append(sha) or "error"
        with mock.patch.object(merge_branches.time, "sleep"):
            self.assertEqual(bp.ci_status_settled("deadbeef", tries=3), "error")
        self.assertEqual(len(looks), 3)


class PickedFromMaster(unittest.TestCase):
    """A backport's trailer has to name a sha that exists in apache/spark."""

    def backporter(self, landings=None, upstream_hits=()):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(upstream="apache-github")
        bp.ledger_commits = {}
        bp.master_landings = dict(landings or {})
        # every commit here is "the same change" except where the id says not
        bp.commit_patch_id = lambda commit: "patch-1" if commit != "unrelated" else "other"
        bp.git = SimpleNamespace(
            has_ref=lambda ref: True,
            subject=lambda rev: "a subject long enough to be specific",
            lines=lambda *args: list(upstream_hits),
        )
        return bp

    def source(self):
        src = Source("my-fix")
        src.base, src.commits = "base", ["local-sha"]
        return src

    def test_a_landing_from_this_run_is_found_by_patch_id(self):
        # upstream/master is as stale as the fetch at the start of the run, so
        # nothing pushed since is in it -- but we know what we pushed.
        bp = self.backporter(landings={"patch-1": "master-sha"})
        self.assertEqual(bp.on_master_already(self.source()), ["master-sha"])

    def test_without_one_it_falls_back_to_searching_upstream(self):
        bp = self.backporter(upstream_hits=["upstream-sha"])
        self.assertEqual(bp.on_master_already(self.source()), ["upstream-sha"])

    def test_a_different_change_with_the_same_subject_is_not_it(self):
        bp = self.backporter(upstream_hits=["unrelated"])
        self.assertEqual(bp.on_master_already(self.source()), [])

    def test_remembering_a_landing_keys_it_by_patch_id(self):
        bp = self.backporter()
        bp.remember_master_landing(self.source(), ["master-sha"])
        self.assertEqual(bp.master_landings, {"patch-1": "master-sha"})

    def test_a_short_landing_list_is_not_paired_up_blindly(self):
        bp = self.backporter()
        src = self.source()
        src.commits = ["one", "two"]
        bp.remember_master_landing(src, ["only-one"])
        self.assertEqual(bp.master_landings, {})


class MasterAlreadyCarriesIt(unittest.TestCase):
    """master adding nothing is not the same as master not having it."""

    def run_execute(self, master_result, on_master):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(upstream="apache-github")
        bp.reports = SimpleNamespace(write=lambda *a: None)
        bp._checkout_failed = set()
        bp.record = lambda *a: None
        bp.ensure_target = lambda target: True
        bp.on_master_already = lambda src: list(on_master)
        self.picked_from = {}

        def backport(src, target, from_commits=None, ci_stand_in=False):
            self.picked_from[target] = from_commits
            return list(master_result) if target == "master" else ["landed"]

        bp.backport = backport
        src = Source("my-fix")
        src.base, src.commits = "base", ["local-sha"]
        bp.execute(src, ["master", "branch-4.1"])
        return self.picked_from

    def test_a_master_we_cannot_check_out_still_says_what_is_up_there(self):
        # master was skipped up front because it was in the list; now it is not
        # going to happen, so the release branches still want the sha upstream
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(upstream="apache-github")
        bp.reports = SimpleNamespace(write=lambda *a: None)
        bp._checkout_failed = set()
        bp.record = lambda *a: None
        bp.ensure_target = lambda target: target != "master"
        bp.on_master_already = lambda src: ["upstream-master-sha"]
        picked = {}
        bp.backport = lambda src, target, from_commits=None: (
            picked.__setitem__(target, from_commits) or ["landed"])
        src = Source("myfix")
        src.base, src.commits, src.base_branch = "base", ["c1"], "master"
        bp.execute(src, ["master", "branch-4.1"])
        self.assertEqual(picked["branch-4.1"], ["upstream-master-sha"])

    def test_the_release_branch_is_picked_from_what_landed_on_master(self):
        picked = self.run_execute(master_result=["fresh-master-sha"], on_master=[])
        self.assertEqual(picked["branch-4.1"], ["fresh-master-sha"])

    def test_master_adding_nothing_falls_back_to_the_sha_already_there(self):
        # backport() found the change already on master and moved on; the
        # release branches still have to name that commit, not a local one.
        picked = self.run_execute(master_result=[], on_master=["older-master-sha"])
        self.assertEqual(picked["branch-4.1"], ["older-master-sha"])

    def test_the_master_sha_is_asked_for_as_late_as_it_can_be(self):
        # another cut of the same work may be pushing to master right now, and
        # under -j >1 it is doing so in parallel -- so the question is asked at
        # the first target that would use the answer, not at entry
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(upstream="apache-github")
        bp.reports = SimpleNamespace(write=lambda *a: None)
        bp.record = lambda *a: None
        order = []
        bp.ensure_target = lambda target: True
        bp.on_master_already = lambda src: order.append("asked") or ["sha"]
        bp.backport = lambda src, target, from_commits=None: (
            order.append(f"backport:{target}") or ["landed"])
        src = Source("myfix-4.1")
        src.base, src.commits, src.base_branch = "base", ["c1"], "master"
        bp.execute(src, ["branch-4.1", "branch-3.5"])
        self.assertEqual(order[0], "asked")
        self.assertEqual(order.count("asked"), 1)   # once, not once per target

    def test_a_rolled_back_master_leaves_the_trailer_off(self):
        # nothing on master and nothing to find: no sha to point at, so the
        # release branch is picked from the local commit with no -x line.
        picked = self.run_execute(master_result=[], on_master=[])
        self.assertIsNone(picked["branch-4.1"])


class ReviewGate(unittest.TestCase):
    """Nothing is pushed that no reviewer -- claude or human -- has looked at."""

    def gate(self, **flags):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(sanity=True, dry_run=False, jobs=1,
                                 operator_review=True, **flags)
        bp.claude_alive = False
        self.written = []
        bp.reports = SimpleNamespace(write=lambda name, *cols: self.written.append(name))
        bp.claude_verdict = lambda *a: self.fail("claude was asked for a verdict")
        return bp

    def test_your_up_front_approval_stands_in_for_claude(self):
        bp = self.gate()
        self.assertTrue(bp.sanity_check("topic", "master", "a..b"))
        self.assertEqual(self.written, [])

    def test_with_no_reviewer_at_all_nothing_is_pushed(self):
        # --no-operator-review means no diff was shown, and -j >1 means there is
        # no terminal to fall back on: claude was the only reviewer left.
        bp = self.gate()
        bp.cfg.operator_review = False
        bp.cfg.jobs = 4
        self.assertFalse(bp.sanity_check("topic", "master", "a..b"))
        self.assertEqual(self.written, ["NEEDS_REVIEW.txt"])

    def test_a_dry_run_stands_in_a_verdict_instead_of_paying_for_one(self):
        bp = self.gate()
        bp.cfg.dry_run = True
        bp.claude_alive = True          # even so, it is not asked
        self.assertTrue(bp.sanity_check("topic", "master", "a..b"))
        self.assertEqual(self.written, [])

    def test_the_gate_can_be_switched_off_outright(self):
        bp = self.gate()
        bp.cfg.sanity = False
        bp.claude_alive = True
        self.assertTrue(bp.sanity_check("topic", "master", "a..b"))
        self.assertEqual(self.written, [])


class CherryPickTrailer(unittest.TestCase):
    """The -x trailer check has to say which commit each trailer names."""

    def make(self, commits, grepped):
        """`grepped` maps a sha to whether the target carries a trailer for it."""
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(upstream="apache-github")
        bp.ledger_commits = {}
        bp.on_master_already = lambda src: []
        bp.commit_patch_id = lambda commit: None

        def lines(*args):
            grep = next(a for a in args if a.startswith("--grep="))
            sha = grep.rsplit(" ", 1)[-1]
            return ["found"] if grepped.get(sha) else []

        bp.git = SimpleNamespace(
            has_ref=lambda ref: True,
            lines=lambda *args: [] if args[0] == "cherry" else lines(*args),
            subject=lambda rev: "",
        )
        src = Source("topic")
        src.fork_name, src.fork_head = "topic", commits[-1]
        src.base, src.commits = "base", list(commits)
        return bp, src

    def test_a_trailer_for_every_commit_is_a_skip(self):
        bp, src = self.make(["c1", "c2"], {"c1": True, "c2": True})
        self.assertEqual(bp.already_landed("master", src)[0], "skip")

    def test_a_trailer_for_one_commit_does_not_cover_the_others(self):
        bp, src = self.make(["c1", "c2"], {"c2": True})
        self.assertIsNone(bp.already_landed("master", src))

    def test_the_single_commit_case_still_works(self):
        bp, src = self.make(["c1"], {"c1": True})
        self.assertEqual(bp.already_landed("master", src)[0], "skip")


class WhatTheBuildIsAsked(unittest.TestCase):
    """A backport is asked whether it still builds; a CI stand-in is asked more."""

    def compile_with(self, ci_stand_in):
        import merge_branches
        from types import SimpleNamespace
        from unittest import mock

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(do_compile=True, dry_run=False,
                                 sbt_tasks=["compile", "Test/compile"],
                                 ci_standin_tasks=["test"],
                                 sbt_timeout=7200, ci_standin_timeout=21600,
                                 log_dir=Path(tempfile.mkdtemp()), sbt=None)
        bp.stamp = "stamp"
        bp.git = SimpleNamespace(root=Path("."), short=lambda rev: "abc1234",
                                 out=lambda *a: "", run=lambda *a, **k: None)
        bp.sbt_launcher = lambda: Path("/bin/true")
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["timeout"] = kwargs.get("timeout")
            return SimpleNamespace(returncode=0)

        with mock.patch.object(merge_branches.subprocess, "run", fake_run):
            bp.compile_target("topic", "master", "HEAD~1", ci_stand_in)
        return seen

    def test_a_backport_only_has_to_build(self):
        seen = self.compile_with(False)
        self.assertEqual(seen["argv"][1:], ["-batch", "compile", "Test/compile"])
        self.assertEqual(seen["timeout"], 7200)

    def test_the_two_runs_do_not_share_a_log(self):
        # the stand-in's log is the only record that the tests were ever run, and
        # the backport check on the same target would open it with "w"
        import merge_branches
        self.assertNotEqual(merge_branches.ref_slug("topic--master"),
                            merge_branches.ref_slug("topic--master--ci-standin"))

    def test_a_ci_stand_in_runs_the_tests(self):
        # nothing here has been run anywhere, so a compile answers none of what
        # CI would have
        seen = self.compile_with(True)
        self.assertEqual(seen["argv"][1:], ["-batch", "test"])
        self.assertEqual(seen["timeout"], 21600)


class NothingFailsUntilItFailsFromClean(unittest.TestCase):
    """The worktree's sbt state is whatever the previous branch left, so a failed
    build -- and a failed test run, which is the one that costs hours -- is run
    once more from clean before it is believed.  A timeout is not: from clean it
    only takes longer."""

    def compile_with(self, ci_stand_in, outcomes, tasks=None):
        """outcomes: one per sbt run, a return code or ("timeout"|"[error] line", code)."""
        from types import SimpleNamespace
        from unittest import mock

        import merge_branches

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(do_compile=True, dry_run=False,
                                 sbt_tasks=tasks or ["compile", "Test/compile"],
                                 ci_standin_tasks=tasks or ["test"],
                                 sbt_timeout=7200, ci_standin_timeout=21600,
                                 log_dir=Path(tempfile.mkdtemp()), sbt=None)
        bp.stamp = "stamp"
        bp.git = SimpleNamespace(root=Path("."), short=lambda rev: "abc1234",
                                 out=lambda *a: "", run=lambda *a, **k: None)
        bp.sbt_launcher = lambda: Path("/bin/true")
        runs = []

        def fake_run(argv, **kwargs):
            runs.append(list(argv)[1:])
            outcome = outcomes[len(runs) - 1]
            if outcome == "timeout":
                raise merge_branches.subprocess.TimeoutExpired(argv, kwargs["timeout"])
            if isinstance(outcome, tuple):
                kwargs["stdout"].write(outcome[0] + "\n")
                outcome = outcome[1]
            return SimpleNamespace(returncode=outcome)

        with mock.patch.object(merge_branches.subprocess, "run", fake_run):
            failure = bp.compile_target("topic", "master", "HEAD~1", ci_stand_in)
        return failure, runs

    def test_a_failed_build_is_run_again_from_clean(self):
        failure, runs = self.compile_with(False, [1, 0])
        self.assertIsNone(failure)
        self.assertEqual(runs, [["-batch", "compile", "Test/compile"],
                                ["-batch", "clean", "compile", "Test/compile"]])

    def test_a_failed_test_run_is_run_again_from_clean_too(self):
        # it used to be the compile only, on the grounds that a red test is
        # usually real; a test run against a jar the previous branch left is not
        failure, runs = self.compile_with(True, [1, 0])
        self.assertIsNone(failure)
        self.assertEqual(runs, [["-batch", "test"], ["-batch", "clean", "test"]])

    def test_failing_from_clean_too_is_believed(self):
        failure, runs = self.compile_with(True, [1, 1])
        self.assertIsNotNone(failure)
        self.assertEqual(len(runs), 2)

    def test_the_errors_kept_are_from_the_run_from_clean(self):
        # the first run may have failed on the stale state, which is exactly
        # what the second was there to discount
        failure, _ = self.compile_with(True, [("[error] stale", 1), ("[error] real", 1)])
        self.assertEqual(failure.first_error, "[error] real")

    def test_a_timeout_is_not_run_again(self):
        # from clean it only takes longer, and the budget is per run
        failure, runs = self.compile_with(True, ["timeout"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(failure.first_error, "timed out after 21600s")

    def test_a_build_already_from_clean_is_not_run_twice(self):
        failure, runs = self.compile_with(True, [1], tasks=["clean", "test"])
        self.assertIsNotNone(failure)
        self.assertEqual(runs, [["-batch", "clean", "test"]])

    def test_it_cannot_be_switched_off(self):
        import contextlib
        import io

        import merge_branches
        stderr = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(stderr):
            merge_branches.parse_args(["--no-clean-retry"])
        self.assertIn("--no-clean-retry was removed", stderr.getvalue())


class AStandInIsBothOfItsRuns(unittest.TestCase):
    """The wait arithmetic charges a stand-in for the run from clean it may need."""

    def test_the_cost_counts_the_run_from_clean(self):
        from types import SimpleNamespace

        import merge_branches
        cfg = SimpleNamespace(ci_standin_tasks=["test"], ci_standin_timeout=100)
        self.assertEqual(merge_branches.stand_in_seconds(cfg), 200)

    def test_unless_the_tasks_already_run_from_clean(self):
        from types import SimpleNamespace

        import merge_branches
        cfg = SimpleNamespace(ci_standin_tasks=["clean", "test"], ci_standin_timeout=100)
        self.assertEqual(merge_branches.stand_in_seconds(cfg), 100)

    def test_one_run_is_six_hours_by_default(self):
        import merge_branches
        self.assertEqual(merge_branches.parse_args([]).ci_standin_timeout, 21600)
        self.assertEqual(merge_branches.parse_args(["--ci-standin-timeout", "18000"])
                         .ci_standin_timeout, 18000)


class TheStandInGatesTheMerge(unittest.TestCase):
    """The tests run on the branch, once, before anything is pushed anywhere."""

    def make(self, failure):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(local_stand_in=True, do_compile=True,
                                 fork_remote="fork", ci_standin_tasks=["test"],
                                 ci_standin_timeout=60, ci_wait_hours=12.0,
                                 log_dir=Path("."))
        self.events = []
        bp.reports = SimpleNamespace(
            write=lambda name, *cols: self.events.append(f"report:{name}"))
        bp._checkout_failed = set()
        bp.record = lambda name, target, why: None
        bp.git = SimpleNamespace(ok=lambda *a: True, why=lambda: "")
        bp.execute = lambda src, targets: self.events.append("merge")
        bp.stand_in_for_ci = lambda src: (self.events.append("test") or failure)
        src = Source("a")
        src.fork_name = "a"
        return bp, [(src, ["master", "branch-4.1"])]

    def test_the_tests_run_before_any_merging(self):
        bp, work = self.make(None)
        self.assertEqual(bp.build_instead_of_waiting(work), "a")
        self.assertEqual(self.events, ["test", "merge"])

    def test_a_checkout_that_fails_is_not_a_failed_test_run(self):
        # nothing was built and nothing was run, so calling it failed would drop
        # a branch whose CI may go green minutes later
        bp, work = self.make(None)
        bp.git.ok = lambda *a: False
        bp.git.why = lambda: "your local changes would be overwritten"
        self.assertIsNone(bp.build_instead_of_waiting(work))   # no turn taken
        self.assertEqual(self.events, [])                      # nothing reported

    def test_a_failure_merges_nothing_at_all(self):
        # not "master went out and then 4.1 failed" -- nothing is pushed
        import merge_branches
        bp, work = self.make(merge_branches.CompileFailure(Path("log"), "ref", "boom"))
        self.assertEqual(bp.build_instead_of_waiting(work), "a")
        self.assertEqual(self.events, ["test", "report:COMPILE_FAILED.txt"])
        self.assertNotIn("merge", self.events)


class StandInForCi(unittest.TestCase):
    """What gets tested, and with which tasks."""

    def run_it(self):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(log_dir=Path("."))
        self.compiled = {}

        def compile_target(src, target, pre_pick, ci_stand_in=False):
            self.compiled = dict(src=src, target=target, pre_pick=pre_pick,
                                 ci_stand_in=ci_stand_in)
            return None

        bp.compile_target = compile_target
        src = Source("myfix-aok")
        src.head, src.base, src.base_branch = "head1", "base1", "branch-4.1"
        return bp.stand_in_for_ci(src)

    def test_it_asks_for_the_ci_task_list(self):
        self.run_it()
        self.assertTrue(self.compiled["ci_stand_in"])

    def test_it_builds_against_the_base_it_was_cut_from(self):
        # which is what picks the JDK for it
        self.run_it()
        self.assertEqual(self.compiled["target"], "branch-4.1")

    def test_it_measures_the_range_from_that_base(self):
        self.run_it()
        self.assertEqual(self.compiled["pre_pick"], "base1")


class TheStandInFitsTheWait(unittest.TestCase):
    def stand_in(self, timeout, deadline):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(local_stand_in=True, do_compile=True,
                                 fork_remote="fork", ci_standin_tasks=["test"],
                                 ci_standin_timeout=timeout, ci_wait_hours=12.0,
                                 log_dir=Path("."))
        ran = []
        bp.git = SimpleNamespace(ok=lambda *a: True, why=lambda: "")
        bp._checkout_failed = set()
        bp.record = lambda *a: None
        bp.execute = lambda src, targets: ran.append(src.name)
        bp.stand_in_for_ci = lambda src: None
        src = Source("a")
        src.fork_name = "a"
        return bp.build_instead_of_waiting([(src, ["master"])], deadline), ran

    def test_one_that_fits_runs(self):
        import time
        nudged, ran = self.stand_in(60, time.time() + 3600)
        self.assertEqual((nudged, ran), ("a", ["a"]))

    def test_the_fit_check_is_measured_against_the_outer_bound(self):
        # against the wait, a 6h cap refused every stand-in a --ci-wait of 6h or
        # less could ever offer -- silently, in a run that never asked for
        # --no-local-build-stand-in.  The outer bound already has room for one
        # stand-in per branch, which is the budget the question is really about.
        import merge_branches
        from types import SimpleNamespace
        from unittest import mock

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(dry_run=False, fork_remote="fork",
                                 ci_poll_minutes=0, ci_wait_hours=1.0,
                                 local_stand_in=True, do_compile=True,
                                 ci_standin_tasks=["test"], ci_standin_timeout=21600)
        bp.git = SimpleNamespace(run=lambda *a, **k: None)
        bp.reports = SimpleNamespace(write=lambda *a: None)
        bp._checkout_failed = set()
        bp.record = lambda *a: None
        bp.warn_if_untested = lambda src: None
        bp.merge_all = lambda work: None
        bp.resolve_on_fork = lambda name: (name, name + "-head")
        bp.git.rev_parse = lambda ref: ref.rsplit("/", 1)[-1].split("^")[0] + "-head"
        bp._gh_complained = set()
        bp.ci_status_of = lambda sha: "running"
        offered = []
        bp.build_instead_of_waiting = lambda queue, deadline=None: (
            offered.append(deadline) or queue[0][0].name)
        now = time.time()
        with mock.patch.object(merge_branches.time, "sleep", lambda s: None):
            bp.revisit([(self.src("a"), ["master"])])
        # a 1h wait would refuse a 6h cap outright; the bound it is given has to
        # be the one with room for the stand-in in it
        self.assertTrue(offered, "the stand-in was never offered a branch")
        self.assertGreater(offered[0], now + 21600)

    def test_a_branch_it_cannot_check_out_does_not_starve_the_others(self):
        # it keeps its place in the poll, but always taking pending[0] meant one
        # unusable branch stopped every other one from ever being tested
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(local_stand_in=True, do_compile=True,
                                 fork_remote="fork", ci_standin_tasks=["test"],
                                 ci_standin_timeout=60, ci_wait_hours=12.0,
                                 log_dir=Path("."))
        bp._checkout_failed = set()
        bp.record = lambda *a: None
        bp.reports = SimpleNamespace(write=lambda *a: None)
        tested = []
        bp.stand_in_for_ci = lambda src: None
        bp.execute = lambda src, targets: tested.append(src.name)
        # "a" can never be checked out; "b" can
        bp.git = SimpleNamespace(ok=lambda *args: args[-1] != "a-head",
                                 why=lambda: "untracked files in the way")

        def source(name):
            src = Source(name)
            src.fork_name, src.head = name, name + "-head"
            return src

        work = [(source("a"), ["master"]), (source("b"), ["master"])]
        self.assertIsNone(bp.build_instead_of_waiting(work))   # "a" fails
        self.assertEqual(bp.build_instead_of_waiting(work), "b")
        self.assertEqual(tested, ["b"])

    def test_one_that_does_not_fit_is_left_to_the_poll(self):
        # nothing else is checked while it runs, so overrunning the wait holds
        # every other branch out of its own CI check too
        import time
        nudged, ran = self.stand_in(21600, time.time() + 600)
        self.assertEqual((nudged, ran), (None, []))

    def test_the_room_it_needs_includes_the_run_from_clean(self):
        # one run fits in 90s at a 60s cap; the run from clean a failure gets
        # does not, and a failure is when it is needed
        import time
        nudged, ran = self.stand_in(60, time.time() + 90)
        self.assertEqual((nudged, ran), (None, []))

    def test_the_others_are_given_back_the_time_it_took(self):
        # --ci-wait is how long to keep coming back to a branch whose CI is
        # still going; merging in the middle of that is not them being slow
        import merge_branches
        from types import SimpleNamespace
        from unittest import mock

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(dry_run=False, fork_remote="fork",
                                 ci_poll_minutes=0, ci_wait_hours=1.0,
                                 local_stand_in=True, do_compile=True,
                                 ci_standin_tasks=["test"],
                                 ci_standin_timeout=21600)
        seen = []
        bp.git = SimpleNamespace(run=lambda *a, **k: None)
        bp.reports = SimpleNamespace(write=lambda name, *cols: seen.append(name))
        bp._checkout_failed = set()
        bp.record = lambda *a: None
        bp.warn_if_untested = lambda src: None
        bp.merge_all = lambda work: None
        bp.resolve_on_fork = lambda name: (name, name + "-head")
        bp.git.rev_parse = lambda ref: ref.rsplit("/", 1)[-1].split("^")[0] + "-head"
        bp._gh_complained = set()
        bp.ci_status_of = lambda sha: "running"     # nothing ever settles
        clock = [1000.0]

        def nudge(queue, deadline=None):
            clock[0] += 3600           # the merge took the whole wait
            return queue[0][0].name

        bp.build_instead_of_waiting = nudge
        with mock.patch.object(merge_branches.time, "time", lambda: clock[0]), \
                mock.patch.object(merge_branches.time, "sleep", lambda s: None):
            bp.revisit([(self.src("a"), ["master"]), (self.src("b"), ["master"])])
        # Each round nudges one branch and the merge eats the whole wait.  Charged
        # to the others, the second round finds the deadline already gone and
        # writes "a" off as having outlasted it; credited back, "a" gets its turn.
        self.assertEqual(seen, [])

    def src(self, name):
        src = Source(name)
        src.fork_name, src.fork_head = name, name + "-head"
        return src

    def test_the_credit_does_not_run_forever(self):
        # crediting each stand-in back restores the whole wait, so without an
        # outer bound the credits compound and "gave up after 12h" prints days on
        import merge_branches
        from types import SimpleNamespace
        from unittest import mock

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(dry_run=False, fork_remote="fork",
                                 ci_poll_minutes=0, ci_wait_hours=1.0,
                                 local_stand_in=True, do_compile=True,
                                 ci_standin_tasks=["test"], ci_standin_timeout=60)
        seen = []
        bp.git = SimpleNamespace(run=lambda *a, **k: None)
        bp.reports = SimpleNamespace(write=lambda name, *cols: seen.append(name))
        bp._checkout_failed = set()
        bp.record = lambda *a: None
        bp.warn_if_untested = lambda src: None
        bp.merge_all = lambda work: None
        bp.resolve_on_fork = lambda name: (name, name + "-head")
        bp.git.rev_parse = lambda ref: ref.rsplit("/", 1)[-1].split("^")[0] + "-head"
        bp._gh_complained = set()
        bp.ci_status_of = lambda sha: "running"      # nothing ever settles
        clock = [1000.0]

        def nudge(queue, deadline=None):
            clock[0] += 3600            # each merge costs the whole wait
            return queue[0][0].name

        bp.build_instead_of_waiting = nudge
        with mock.patch.object(merge_branches.time, "time", lambda: clock[0]), \
                mock.patch.object(merge_branches.time, "sleep", lambda s: None):
            bp.revisit([(self.src(n), ["master"]) for n in "abc"])
        # Each round credits the hour back, so the soft deadline never arrives.
        # The outer bound does, and the last branch is written off rather than
        # the run going on for as long as there are branches to nudge.
        self.assertEqual(seen, ["TO_MERGE_LATER.txt"])

    def test_with_no_deadline_it_always_runs(self):
        nudged, ran = self.stand_in(21600, None)
        self.assertEqual((nudged, ran), ("a", ["a"]))


class AnAdoptedCutIsFoundAgain(unittest.TestCase):
    """It is named for its local rewrite, which the fork has never carried."""

    def make(self):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(fork_remote="origin", strip_suffixes=["-aok"])
        on_fork = {"refs/remotes/origin/myfix-branch-4.1^{commit}": "forkhead"}
        bp.git = SimpleNamespace(rev_parse=lambda ref: on_fork.get(ref))
        src = Source("myfix-4.1-aok")
        src.fork_name, src.fork_head = "myfix-branch-4.1", "forkhead"
        return bp, src

    def test_it_is_looked_up_under_the_name_the_fork_has(self):
        # resolve_on_fork would try myfix-4.1-aok and myfix-4.1, neither of which
        # is there, and report the branch as gone -- while sibling_coverage has
        # already taken its release off the branch that was listed
        bp, src = self.make()
        self.assertEqual(bp.fork_ref_of(src), ("myfix-branch-4.1", "forkhead"))

    def test_a_cut_that_really_did_vanish_still_reads_as_gone(self):
        bp, src = self.make()
        bp.git.rev_parse = lambda ref: None
        self.assertIsNone(bp.fork_ref_of(src))

    def test_squash_magic_is_handed_that_name_too(self):
        bp, src = self.make()
        self.assertEqual(bp.refresh_name(src), "myfix-branch-4.1")

    def test_a_listed_branch_is_still_resolved_the_usual_way(self):
        bp, src = self.make()
        src.fork_name = ""
        bp.resolve_on_fork = lambda name: ("resolved-" + name, "h")
        self.assertEqual(bp.fork_ref_of(src), ("resolved-myfix-4.1-aok", "h"))
        self.assertEqual(bp.refresh_name(src), "myfix-4.1")


class MasterCutGoesFirst(unittest.TestCase):
    def test_a_master_pinned_cut_is_merged_before_the_others(self):
        # it lands the sha the other cuts of the same work want to name, and
        # master_landings only has it once that cut has pushed
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(strip_suffixes=["-aok"], jobs=1)
        order = []
        bp.execute = lambda src, targets: order.append(src.name)
        work = [(Source("myfix-aok"), ["branch-4.1"]),
                (Source("other"), ["branch-3.5"]),
                (Source("myfix-master-aok"), ["master"])]
        bp.merge_all(work)
        self.assertEqual(order[0], "myfix-master-aok")

    def test_the_rest_keep_their_order(self):
        import merge_branches
        from types import SimpleNamespace

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(strip_suffixes=["-aok"], jobs=1)
        order = []
        bp.execute = lambda src, targets: order.append(src.name)
        bp.merge_all([(Source(n), ["master"]) for n in ("a", "b", "c")])
        self.assertEqual(order, ["a", "b", "c"])


class RemovedFlags(unittest.TestCase):
    def test_assume_yes_is_refused_with_a_reason(self):
        import contextlib, io
        import merge_branches
        stderr = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(stderr):
            merge_branches.parse_args(["-y"])
        self.assertIn("was removed", stderr.getvalue())

    def test_it_is_not_advertised_in_the_help(self):
        import contextlib, io
        import merge_branches
        stdout = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(stdout):
            merge_branches.parse_args(["--help"])
        self.assertNotIn("assume-yes", stdout.getvalue())


class RefSlug(unittest.TestCase):
    def test_slashes_and_spaces_become_underscores(self):
        self.assertEqual(ref_slug("feature/my branch--master"), "feature_my_branch--master")

    def test_safe_characters_survive(self):
        self.assertEqual(
            ref_slug("xss-fix-branch-3.5--branch-3.5"), "xss-fix-branch-3.5--branch-3.5"
        )


class CiVerdicts(unittest.TestCase):
    """One mapping from a CI state to what the run does with it."""

    def test_every_state_ci_status_can_return_has_a_verdict(self):
        from merge_branches import ci_verdict

        self.assertEqual(ci_verdict("passing"), "green")
        self.assertEqual(ci_verdict("running"), "building")
        self.assertEqual(ci_verdict("running-with-failures"), "red")
        self.assertEqual(ci_verdict("failing"), "red")
        self.assertEqual(ci_verdict("none"), "unknown")
        self.assertEqual(ci_verdict("error"), "unknown")

    def test_a_state_nobody_has_heard_of_is_not_a_reason_to_merge(self):
        from merge_branches import ci_verdict

        self.assertEqual(ci_verdict("queued"), "unknown")


class WhyNoStandIn(unittest.TestCase):
    """The stand-in's "when", in words, so the log can say it every round."""

    def why(self, **overrides):
        from merge_branches import IDLE_ROUNDS_BEFORE_STANDING_IN, why_no_stand_in

        args = dict(enabled=True, can_build=True,
                    quiet_rounds=IDLE_ROUNDS_BEFORE_STANDING_IN, went_green=0, building=5)
        args.update(overrides)
        return why_no_stand_in(**args)

    def test_a_stalled_queue_gets_a_test_run(self):
        self.assertIsNone(self.why())

    def test_switched_off_names_the_flag(self):
        self.assertIn("--no-local-build-stand-in", self.why(enabled=False))

    def test_no_build_says_there_is_nothing_to_stand_in_with(self):
        self.assertIn("--no-compile", self.why(can_build=False))

    def test_too_few_quiet_rounds_says_how_many_it_takes(self):
        from merge_branches import IDLE_ROUNDS_BEFORE_STANDING_IN

        why = self.why(quiet_rounds=1)
        self.assertIn("1 quiet round", why)
        self.assertIn(f"after {IDLE_ROUNDS_BEFORE_STANDING_IN}", why)

    def test_a_green_round_says_the_count_started_over(self):
        why = self.why(quiet_rounds=0, went_green=2)
        self.assertIn("2 went green", why)
        self.assertIn("starts over", why)

    def test_only_misreads_left_says_nothing_is_cleanly_queued(self):
        self.assertIn("cleanly queued", self.why(building=0))

    def test_the_flags_come_before_the_count(self):
        # "1 quiet round so far" under --no-compile promises a test run that
        # can never come
        self.assertIn("--no-compile", self.why(can_build=False, quiet_rounds=1))


class WaitBudgetRules(unittest.TestCase):
    """The two bounds on a wait, and what a stand-in's time does to each."""

    def budget(self, wait_hours, standin_seconds=60, branches=3):
        from unittest import mock

        import merge_branches

        self.clock = [1000.0]
        self.patch = mock.patch.object(merge_branches.time, "time", lambda: self.clock[0])
        self.patch.start()
        self.addCleanup(self.patch.stop)
        return merge_branches.WaitBudget(wait_hours, standin_seconds, branches)

    def test_no_wait_is_no_bound(self):
        budget = self.budget(0)
        self.clock[0] += 10 ** 9
        self.assertFalse(budget.over())

    def test_the_wait_runs_out(self):
        budget = self.budget(1.0)
        self.clock[0] = 1000 + 3599
        self.assertFalse(budget.over())
        self.clock[0] = 1000 + 3600
        self.assertTrue(budget.over())

    def test_a_stand_in_is_credited_back_to_the_wait(self):
        budget = self.budget(1.0, standin_seconds=7200)
        budget.credit(3600)
        self.clock[0] = 1000 + 3600
        self.assertFalse(budget.over())
        self.clock[0] = 1000 + 7200
        self.assertTrue(budget.over())

    def test_the_outer_bound_takes_no_credit(self):
        # 1h of waiting plus three 60s stand-ins is the most this run may take,
        # however much the wait is credited back
        budget = self.budget(1.0, standin_seconds=60, branches=3)
        budget.credit(3600)
        budget.credit(3600)
        self.clock[0] = 1000 + 3600 + 180
        self.assertTrue(budget.over())

    def test_the_outer_bound_has_room_for_one_stand_in_per_branch(self):
        budget = self.budget(1.0, standin_seconds=21600, branches=10)
        self.assertEqual(budget.hard_deadline, budget.deadline + 10 * 21600)

    def test_it_reports_how_long_it_has_waited(self):
        budget = self.budget(12.0)
        self.clock[0] = 1000 + 5400
        self.assertAlmostEqual(budget.waited_hours(), 1.5)


class OneRoundOfPolling(unittest.TestCase):
    """poll_once() sorts one look at GitHub by what happens to each branch next."""

    def make(self, states):
        from types import SimpleNamespace

        import merge_branches

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(fork_remote="fork", fork_repo="holdenk/spark")
        bp.git = SimpleNamespace(run=lambda *a, **k: None)
        self.written = []
        bp.reports = SimpleNamespace(
            write=lambda name, branch, *cols: self.written.append((name, branch)))
        bp.record = lambda *a: None
        bp.warn_if_untested = lambda src: None
        bp.resolve_on_fork = lambda name: (name, name + "-head")
        bp.git.rev_parse = lambda ref: ref.rsplit("/", 1)[-1].split("^")[0] + "-head"
        bp.ci_status_settled = lambda sha: states[sha]
        return bp

    def source(self, name, fork_head=None):
        src = Source(name)
        src.fork_name, src.fork_head = name, fork_head or name + "-head"
        return src

    def names(self, pairs):
        return [src.name for src, _ in pairs]

    def test_each_answer_goes_to_its_own_pile(self):
        from collections import defaultdict

        bp = self.make({"a-head": "passing", "b-head": "running",
                        "c-head": "running-with-failures", "d-head": "failing",
                        "e-head": "none"})
        pending = [(self.source(n), ["master"]) for n in "abcde"]
        seen = bp.poll_once(pending, defaultdict(int))
        self.assertEqual(self.names(seen.green), ["a"])
        self.assertEqual(self.names(seen.red), ["c", "d"])
        # the misread stays in the queue but is not offered to a local build
        self.assertEqual(self.names(seen.still), ["b", "e"])
        self.assertEqual(self.names(seen.building), ["b"])
        self.assertEqual(self.written, [])

    def test_a_branch_that_went_green_is_marked_so(self):
        from collections import defaultdict

        bp = self.make({"a-head": "passing"})
        src = self.source("a")
        bp.poll_once([(src, ["master"])], defaultdict(int))
        self.assertTrue(src.ci_green)

    def test_a_branch_that_moved_or_vanished_is_written_off_in_place(self):
        from collections import defaultdict

        bp = self.make({"a-head": "running", "b-head": "running"})
        bp.git.rev_parse = lambda ref: (None if "/gone^" in ref
                                        else ref.rsplit("/", 1)[-1].split("^")[0] + "-head")
        moved = self.source("a", fork_head="a-old")     # the fork now has a-head
        gone = self.source("gone")
        seen = bp.poll_once([(moved, ["master"]), (gone, ["master"]),
                             (self.source("b"), ["master"])], defaultdict(int))
        self.assertEqual(self.names(seen.still), ["b"])
        self.assertEqual(self.written, [("TO_MERGE_LATER.txt", "a"),
                                        ("TO_MERGE_LATER.txt", "gone")])

    def test_misreads_are_counted_across_rounds_and_a_good_read_clears_them(self):
        from collections import defaultdict

        import merge_branches

        misreads = defaultdict(int)
        pending = [(self.source("a"), ["master"])]
        bp = self.make({"a-head": "none"})
        for _ in range(merge_branches.CI_MISREADS_ALLOWED - 1):
            seen = bp.poll_once(pending, misreads)
            self.assertEqual(self.names(seen.still), ["a"])
        bp.ci_status_settled = lambda sha: "running"
        bp.poll_once(pending, misreads)
        self.assertNotIn("a", misreads)
        bp.ci_status_settled = lambda sha: "none"
        for _ in range(merge_branches.CI_MISREADS_ALLOWED - 1):
            seen = bp.poll_once(pending, misreads)
        self.assertEqual(self.names(seen.still), ["a"])           # still not given up on
        seen = bp.poll_once(pending, misreads)
        self.assertEqual(seen.still, [])
        self.assertEqual(self.written, [("TO_MERGE_LATER.txt", "a")])


class TheQuietCountSurvivesAStandIn(unittest.TestCase):
    """The poll straight after a test run is the strongest evidence there is."""

    def make(self, answers):
        from collections import defaultdict
        from types import SimpleNamespace

        import merge_branches

        bp = object.__new__(merge_branches.Backporter)
        bp.cfg = SimpleNamespace(dry_run=False, fork_remote="fork",
                                 fork_repo="holdenk/spark", ci_poll_minutes=0,
                                 ci_wait_hours=0, local_stand_in=True,
                                 do_compile=True, ci_standin_tasks=["test"],
                                 ci_standin_timeout=21600)
        bp.git = SimpleNamespace(run=lambda *a, **k: None)
        bp.reports = SimpleNamespace(write=lambda *a: None)
        bp._checkout_failed = set()
        bp.record = lambda *a: None
        bp.warn_if_untested = lambda src: None
        self.merged = []
        bp.merge_all = lambda work: self.merged.extend(src.name for src, _ in work)
        self.polls = defaultdict(int)
        queues = {sha: list(states) for sha, states in answers.items()}

        def status(sha):
            self.polls[sha] += 1
            return queues[sha].pop(0) if len(queues[sha]) > 1 else queues[sha][0]

        bp.ci_status_of = status
        bp.resolve_on_fork = lambda name: (name, name + "-head")
        bp.git.rev_parse = lambda ref: ref.rsplit("/", 1)[-1].split("^")[0] + "-head"
        bp._gh_complained = set()
        self.tested = []
        bp.build_instead_of_waiting = lambda queue, deadline=None: (
            self.tested.append(queue[0][0].name) or queue[0][0].name)
        return bp

    def source(self, name):
        src = Source(name)
        src.fork_name, src.fork_head = name, name + "-head"
        return src

    def test_a_stalled_queue_is_worked_through_back_to_back(self):
        # Neither ever goes green.  The three quiet rounds buy the first test
        # run; the round after it finds nothing green either, and that is the
        # second -- not three more rounds of sleeping first.
        import merge_branches

        quiet = merge_branches.IDLE_ROUNDS_BEFORE_STANDING_IN
        bp = self.make({"a-head": ["running"], "b-head": ["running"]})
        bp.revisit([(self.source("a"), ["master"]), (self.source("b"), ["master"])])
        self.assertEqual(self.tested, ["a", "b"])
        self.assertEqual(dict(self.polls), {"a-head": quiet, "b-head": quiet + 1})

    def test_a_green_round_still_starts_the_count_over(self):
        # a goes green on the second look: CI is moving, so b waits for three
        # quiet rounds from there before hours are spent on it
        import merge_branches

        quiet = merge_branches.IDLE_ROUNDS_BEFORE_STANDING_IN
        bp = self.make({"a-head": ["running", "passing"], "b-head": ["running"]})
        bp.revisit([(self.source("a"), ["master"]), (self.source("b"), ["master"])])
        self.assertEqual(self.merged, ["a"])
        self.assertEqual(self.tested, ["b"])
        self.assertEqual(self.polls["b-head"], 2 + quiet)


class EveryRoundSaysWhy(unittest.TestCase):
    """A round that only waits says what it is waiting for."""

    def make(self, answers, **cfg):
        from types import SimpleNamespace

        import merge_branches

        bp = object.__new__(merge_branches.Backporter)
        settings = dict(dry_run=False, fork_remote="fork", fork_repo="holdenk/spark",
                        ci_poll_minutes=0, ci_wait_hours=0, local_stand_in=True,
                        do_compile=True, ci_standin_tasks=["test"], ci_standin_timeout=21600)
        settings.update(cfg)
        bp.cfg = SimpleNamespace(**settings)
        bp.git = SimpleNamespace(run=lambda *a, **k: None)
        bp.reports = SimpleNamespace(write=lambda *a: None)
        bp._checkout_failed = set()
        bp.record = lambda *a: None
        bp.warn_if_untested = lambda src: None
        bp.merge_all = lambda work: None
        queues = {sha: list(states) for sha, states in answers.items()}
        bp.ci_status_of = lambda sha: queues[sha].pop(0)
        bp.resolve_on_fork = lambda name: (name, name + "-head")
        bp.git.rev_parse = lambda ref: ref.rsplit("/", 1)[-1].split("^")[0] + "-head"
        bp._gh_complained = set()
        bp.build_instead_of_waiting = lambda queue, deadline=None: None
        return bp

    def run_it(self, bp):
        import contextlib
        import io

        src = Source("a")
        src.fork_name, src.fork_head = "a", "a-head"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bp.revisit([(src, ["master"])])
        return out.getvalue()

    def test_the_first_quiet_round_says_how_many_it_takes(self):
        import merge_branches

        out = self.run_it(self.make({"a-head": ["running", "passing"]}))
        self.assertIn("1 quiet round(s) so far", out)
        self.assertIn(f"after {merge_branches.IDLE_ROUNDS_BEFORE_STANDING_IN}", out)
        self.assertIn("looking again in", out)

    def test_a_run_that_cannot_build_says_so_rather_than_counting(self):
        out = self.run_it(self.make({"a-head": ["running", "passing"]}, do_compile=False))
        self.assertIn("--no-compile", out)
        self.assertNotIn("quiet round", out)

    def test_polling_only_names_its_flag(self):
        out = self.run_it(self.make({"a-head": ["running", "passing"]}, local_stand_in=False))
        self.assertIn("--no-local-build-stand-in", out)

    def test_a_stand_in_that_starts_says_so(self):
        import merge_branches

        quiet = merge_branches.IDLE_ROUNDS_BEFORE_STANDING_IN
        out = self.run_it(self.make({"a-head": ["running"] * quiet + ["passing"]}))
        self.assertIn(f"{quiet} round(s) in a row with nothing going green", out)


if __name__ == "__main__":
    unittest.main()
