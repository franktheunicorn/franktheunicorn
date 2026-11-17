#!/usr/bin/env python3
"""Unit tests for add_coauthor.py.

    python3 -m unittest test_add_coauthor -v
"""

import subprocess
import sys
import unittest

from add_coauthor import COAUTHOR, add

CLAUDE = b"Co-Authored-By: Claude <noreply@anthropic.com>"


class AlreadyThere(unittest.TestCase):
    def test_the_exact_trailer_is_left_alone(self):
        msg = b"Subject\n\n" + COAUTHOR + b"\n"
        self.assertEqual(add(msg), msg)

    def test_a_differently_cased_spelling_counts(self):
        msg = b"Subject\n\nco-authored-by: Holden Karau <holden@pigscanfly.ca>\n"
        self.assertEqual(add(msg), msg)

    def test_somebody_elses_trailer_does_not_count(self):
        self.assertIn(COAUTHOR, add(b"Subject\n\n" + CLAUDE + b"\n"))


class WhereTheTrailerGoes(unittest.TestCase):
    """Placement is git's job; these pin the outcomes that matter."""

    def test_it_joins_an_existing_trailer_block(self):
        # a blank line here would start a new paragraph, and git reads only the
        # last paragraph as trailers -- Claude would stop counting as a co-author
        self.assertEqual(add(b"Subject\n\nBody\n\n" + CLAUDE + b"\n"),
                         b"Subject\n\nBody\n\n" + CLAUDE + b"\n" + COAUTHOR + b"\n")

    def test_a_bare_subject_gets_a_paragraph_of_its_own(self):
        self.assertEqual(add(b"SPARK-1234: a subject with a colon in it\n"),
                         b"SPARK-1234: a subject with a colon in it\n\n"
                         + COAUTHOR + b"\n")

    def test_a_missing_final_newline_is_supplied(self):
        self.assertEqual(add(b"Subject"), b"Subject\n\n" + COAUTHOR + b"\n")

    def test_an_empty_message_is_just_the_trailer(self):
        self.assertEqual(add(b""), COAUTHOR + b"\n")
        self.assertEqual(add(b"\n\n"), COAUTHOR + b"\n")


class AlreadyThereMeansGitSaysSo(unittest.TestCase):
    def test_the_trailer_quoted_in_prose_does_not_count(self):
        # it is in the text but not in the trailer block, so git reports no
        # co-author -- taking it as done would leave the commit with none
        msg = (b"SPARK-1: fix it\n\nThis mirrors what we did before, where\n"
               + COAUTHOR + b"\nwas used.\n\nSigned-off-by: A <a@b.c>\n")
        out = add(msg)
        self.assertNotEqual(out, msg)
        parsed = subprocess.run(["git", "interpret-trailers", "--parse"], input=out,
                                stdout=subprocess.PIPE).stdout
        self.assertIn(COAUTHOR, parsed)
        self.assertIn(b"Signed-off-by: A <a@b.c>", parsed)

    def test_a_real_trailer_still_short_circuits(self):
        msg = b"Subject\n\nBody\n\n" + COAUTHOR + b"\n"
        self.assertEqual(add(msg), msg)


class Encoding(unittest.TestCase):
    def test_bytes_that_are_not_utf8_survive(self):
        # git messages carry whatever encoding they were written in; decoding
        # one would abort the rewrite partway through
        msg = b"Fix caf\xe9 handling\n"
        out = add(msg)
        self.assertTrue(out.startswith(msg))
        self.assertIn(b"\xe9", out)

    def test_it_is_idempotent_on_its_own_output(self):
        once = add(b"Fix caf\xe9 handling\n")
        self.assertEqual(add(once), once)


class AgreesWithGit(unittest.TestCase):
    """The rule is git's, so check it against git rather than against taste.

    The failure that matters is not "the layout looks odd", it is "a trailer
    git used to report is not reported any more".  Nothing we add may cost a
    message a trailer it already had.
    """

    MESSAGES = [
        b"Subject\n\nBody\n\nCo-Authored-By: Claude <n@a.com>\n",
        b"Subject\n\nBody\n\nNote: watch out\n",
        b"Subject\n\nTicket: PROJ-42\n",
        b"Subject\n\nFixes: #123\n",
        b"Subject\n\n(cherry picked from commit abc123)\n",
        b"Subject\n\nCo-authored-by: Somebody\n    <somebody@example.com>\n",
        b"Subject\n\nSigned-off-by: A <a@b.c>\nTicket: X-1\n",
        b"Subject\n\nSome prose here\nMore prose\nNote: x\n",
        b"Subject\n\nThis fixes the bug.\n",
        b"Subject only\n",
        b"Subject\n\nCo-Authored-By:Nospace <n@s.com>\n",
        # prose and a sign-off sharing the last paragraph: git reads that as a
        # trailer block, so a blank line in front of ours drops the DCO line
        b"Subject\n\nThis fixes the thing.\nSigned-off-by: A <a@b.c>\n",
        # a last paragraph starting with an indented line is NOT a trailer
        # block to git, so joining it would leave ours unparsed as well
        b"Subject\n\nRepro:\n\n    foo.bar()\nFixes: #123\n",
        b"Subject\n\nBody\n\nTicket: PROJ-42\n" + CLAUDE + b"\n",
        # no trailing newline: git appends to the *subject line* unless the
        # message is normalised first, and then nothing parses at all
        b"Subject\n\nSigned-off-by: A <a@b.c>",
    ]

    @staticmethod
    def tokens(msg):
        out = subprocess.run(["git", "interpret-trailers", "--parse"], input=msg,
                             stdout=subprocess.PIPE).stdout
        return {line.split(b":")[0].lower() for line in out.splitlines() if line.strip()}

    def test_no_message_loses_a_trailer_git_was_reporting(self):
        for msg in self.MESSAGES:
            with self.subTest(msg=msg.decode()):
                self.assertEqual(self.tokens(msg) - self.tokens(add(msg)), set())

    def test_ours_is_always_reported_afterwards(self):
        """The one thing this tool exists to do."""
        for msg in self.MESSAGES:
            with self.subTest(msg=msg.decode()):
                self.assertIn(b"co-authored-by", self.tokens(add(msg)))


class HostileTrailerConfig(unittest.TestCase):
    """interpret-trailers obeys the operator's trailer.* config; we must not."""

    def under(self, msg, **config):
        import os
        env = dict(os.environ, GIT_CONFIG_COUNT=str(len(config)))
        for number, (key, value) in enumerate(config.items()):
            env[f"GIT_CONFIG_KEY_{number}"] = key.replace("_", ".")
            env[f"GIT_CONFIG_VALUE_{number}"] = value
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "add_coauthor.py")
        done = subprocess.run([sys.executable, script], input=msg,
                              stdout=subprocess.PIPE, env=env, check=True)
        return done.stdout

    WITH_CLAUDE = b"Subject\n\n" + CLAUDE + b"\n"

    def test_if_exists_do_nothing_does_not_silently_add_nothing(self):
        self.assertIn(COAUTHOR, self.under(self.WITH_CLAUDE,
                                           trailer_ifexists="doNothing"))

    def test_if_exists_replace_does_not_delete_the_other_co_author(self):
        out = self.under(self.WITH_CLAUDE, trailer_ifexists="replace")
        self.assertIn(CLAUDE, out)
        self.assertIn(COAUTHOR, out)

    def test_if_missing_do_nothing_still_adds_ours(self):
        self.assertIn(COAUTHOR, self.under(b"Subject\n\nBody\n",
                                           trailer_ifmissing="doNothing"))

    def test_a_separator_setting_does_not_corrupt_the_line(self):
        out = self.under(b"Subject\n\nBody\n", trailer_separators="=")
        self.assertIn(COAUTHOR + b"\n", out)

    def test_a_renamed_token_is_not_taken_for_success(self):
        # the prose quotes our line, so searching the raw output would pass even
        # though git emitted the real trailer under a token of its own choosing
        msg = (b"Subject\n\nThis mirrors what we did before, where\n" + COAUTHOR
               + b"\nwas used.\n\nSigned-off-by: A <a@b.c>\n")
        out = self.under(msg, **{"trailer_co-authored-by_key": "Reported-by: "})
        parsed = subprocess.run(["git", "interpret-trailers", "--parse"], input=out,
                                stdout=subprocess.PIPE).stdout
        self.assertIn(COAUTHOR, parsed)

    def test_a_divider_in_the_body_does_not_strand_the_trailer(self):
        # "---" ends the message as far as interpret-trailers is concerned, so
        # without --no-divider ours lands above it and outside the block
        out = self.under(b"Subject\n\nBody\n\n---\n\nExtra notes\n")
        self.assertTrue(out.rstrip(b"\n").endswith(COAUTHOR), out)


if __name__ == "__main__":
    unittest.main()
