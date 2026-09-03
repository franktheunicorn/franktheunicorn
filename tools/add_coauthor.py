#!/usr/bin/env python3
"""Add Holden as a co-author to the commit message on stdin.

Reads a message, writes it back with the trailer appended, and is a no-op if
the trailer is already there.  Used as a filter (see add_coauthor.sh).

Bytes in, bytes out.  A commit message is whatever encoding it was written in,
not necessarily UTF-8, and a latin-1 name in somebody's 2013 commit has to come
back out exactly as it went in -- decoding it would stop the whole rewrite with
a UnicodeDecodeError halfway through.

Where the trailer goes is git's decision, not ours: `git interpret-trailers`
does the insertion.  Deciding it here means reimplementing git's rule for what
counts as a trailer block, and that rule has more to it than it looks -- a
paragraph of trailers, yes, but also prose followed by a Signed-off-by (joined),
and a paragraph whose first line is an indented continuation (not joined).
Getting it wrong is not cosmetic: put a blank line where git would not and the
new paragraph becomes the trailer block on its own, so every trailer above it --
somebody else's Co-Authored-By, a Ticket:, a DCO sign-off -- stops being read as
one.  Two hand-rolled attempts at this each broke a different shape.
"""
import os
import re
import subprocess
import sys

COAUTHOR = b"Co-Authored-By: Holden Karau <holden@pigscanfly.ca>"

# Ours already, in any spelling: git's own --if-exists matches the token and
# value literally, and the whole point here is to be case-insensitive about the
# header and relaxed about the whitespace around the address.
MINE = re.compile(rb"(?im)^\s*co-authored-by:\s*.*<\s*holden@pigscanfly\.ca\s*>\s*$")

# interpret-trailers reads trailer.* from the operator's config, and several of
# those settings quietly turn this tool into a no-op or worse.  Every one of
# these was reproduced against git rather than guessed at:
#
#   trailer.ifexists=doNothing   a message that already has somebody else's
#                                Co-Authored-By comes back untouched, exit 0 --
#                                the whole run silently adds nothing
#   trailer.ifexists=replace     ...or that line is *deleted* and ours put in
#                                its place, across the rewritten range
#   trailer.ifmissing=doNothing  nothing is ever added
#   trailer.separators="="       the emitted line is "...pigscanfly.ca>= ", and
#                                existing trailers stop being recognised
#   trailer.where=start          ours goes above the block rather than below
#
# So say what we mean on the command line, where it overrides the config.
GIT = [
    "git", "-c", "trailer.separators=:",     # no flag for this one
    "interpret-trailers",
    # ...and do not treat a "---" in the body as the end of the message, which
    # would put the trailer above it and outside the block GitHub reads.
    "--no-divider",
]
GIT_TRAILER = GIT + [
    "--if-exists", "addIfDifferent",
    "--if-missing", "add",
    "--where", "end",
    "--trailer", COAUTHOR,
]
GIT_PARSE = GIT + ["--parse"]


# trailer.<token>.key and trailer.<token>.cmd have no command-line override, so
# a second attempt drops the operator's global and system config on the floor.
# Repo-local config still applies, but these settings live in ~/.gitconfig in
# practice, and this keeps git the authority on placement rather than falling
# back to a rule of our own -- two hand-rolled ones each broke a different shape.
WITHOUT_USER_CONFIG = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def append_alone(body: bytes) -> bytes:
    """The trailer as a paragraph of its own.

    Always a valid trailer block, so ours is always credited.  It loses any
    trailers that were sharing the last paragraph, which is why it is the last
    resort and not the rule.
    """
    return body + b"\n" + COAUTHOR + b"\n"


def run(argv: list, body: bytes, env: dict[str, str] | None = None) -> bytes | None:
    """git's answer, or None if it could not give one."""
    try:
        done = subprocess.run(argv, input=body, stdout=subprocess.PIPE,
                              env={**os.environ, **(env or {})}, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return done.stdout


def already_mine(msg: bytes, env: dict[str, str] | None = None) -> bool:
    """Does git *report* our trailer on this message?

    Not "does the text contain it".  A message whose prose quotes a
    Co-Authored-By line has it in the text and not in the trailer block, and
    reading that as done leaves the commit with no trailer at all -- which is
    the one thing this tool is here to prevent.
    """
    parsed = run(GIT_PARSE, msg, env)
    if parsed is None:
        return bool(MINE.search(msg))      # no git to ask; the old, coarser test
    return bool(MINE.search(parsed))


def interpret(body: bytes, env: dict[str, str] | None = None) -> bytes | None:
    """The message with our trailer in it, or None if that did not happen."""
    out = run(GIT_TRAILER, body, env)
    if out is None:
        return None
    # Do not take the exit code on trust: with trailer.<token>.key or .cmd set,
    # git exits 0 having renamed or swallowed the line we asked for.  Adding
    # nothing quietly is the failure this whole tool is here to avoid.  Asked of
    # git rather than of the text, for the same reason already_mine() is: a
    # message whose prose quotes a Co-Authored-By line satisfies a search of the
    # raw output whatever git actually did with the trailer.
    return out if already_mine(out, env) else None


def add(msg: bytes) -> bytes:
    if already_mine(msg):
        return msg
    body = msg.rstrip(b"\n")
    if not body.strip():
        return COAUTHOR + b"\n"
    # Hand git a message that ends in a newline.  Without one it appends the
    # trailer to the *subject line* rather than after it, and the result is one
    # paragraph starting with a non-trailer -- which git then parses as no
    # trailers at all, ours included.
    body += b"\n"
    for env in (None, WITHOUT_USER_CONFIG):
        answer = interpret(body, env)
        if answer is not None:
            return answer
    # Say so.  This path still credits the co-author, but it starts a new
    # paragraph, and any trailer that was sharing the last one -- somebody
    # else's Co-Authored-By, a Ticket:, a DCO sign-off -- stops being read as
    # one.  Degrading quietly across a whole filter-repo range is worse than
    # degrading loudly, so this goes to stderr where the caller can surface it.
    print("!! git interpret-trailers could not place the trailer (repo-local "
          "trailer.* config?) -- appending it as its own paragraph, which may "
          "drop trailers that shared the last one", file=sys.stderr)
    return append_alone(body)


if __name__ == "__main__":
    sys.stdout.buffer.write(add(sys.stdin.buffer.read()))
