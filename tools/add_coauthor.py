#!/usr/bin/env python3
"""Add Holden as a co-author to the commit message on stdin.

Reads a message, writes it back with the trailer appended, and is a no-op if
the trailer is already there.  Used as a filter (see add_coauthor.sh).
"""
import re
import sys

COAUTHOR = "Co-Authored-By: Holden Karau <holden@pigscanfly.ca>"

# A git trailer line: a token, a colon, a value.  "(cherry picked from commit
# ...)" is not one, but it lives in the trailer block and git tolerates it
# there, so a block ending in one is still a trailer block.
TRAILER = re.compile(r"^[A-Za-z0-9-]+:\s|^\(cherry picked from commit [0-9a-f]+\)$")


def add(msg: str) -> str:
    # Already present (case-insensitive on the header, tolerant on whitespace).
    if re.search(r"(?im)^\s*co-authored-by:\s*.*<\s*holden@pigscanfly\.ca\s*>\s*$", msg):
        return msg

    body = msg.rstrip("\n")
    if not body:
        return COAUTHOR + "\n"

    # A trailer goes *in* the last paragraph when that paragraph is already the
    # trailer block.  A blank line in front of it would start a new paragraph,
    # and git (and GitHub) read only the last one as trailers -- so the
    # Co-Authored-By lines above it would stop counting.
    paragraphs = body.split("\n\n")
    last = paragraphs[-1].splitlines()
    joins_block = len(paragraphs) > 1 and all(TRAILER.match(line) for line in last)
    return body + ("\n" if joins_block else "\n\n") + COAUTHOR + "\n"


if __name__ == "__main__":
    sys.stdout.write(add(sys.stdin.read()))
