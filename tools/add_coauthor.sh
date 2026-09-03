#!/bin/bash
# Add Holden as a co-author to every commit a branch adds on top of somebody
# else's work.  The trailer itself is written by add_coauthor.py, so the two
# agree about what "already there" means and about where in the message a
# trailer goes.
#
# Usage: add_coauthor.sh [<base>]
#
#   <base>  the commit or ref this branch was cut from.  Everything above it is
#           rewritten and nothing at or below it is touched.
#
#           Optional.  With no base it falls back to the newest commit authored
#           by neither you nor Claude, which is only a guess: on a project you
#           commit to yourself, the upstream tip can easily be one of yours, and
#           then the guess walks straight past it and rewrites upstream history.
#           Pass the base whenever you know it -- squash-magic.sh does.
#
# set -e, but deliberately not pipefail: the awk below exits on the first match
# and git log dies of SIGPIPE, which is the intended end of that pipeline.
set -eu

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FILTER="$SCRIPT_DIR/add_coauthor.py"
ALT_AUTHOR_NAME="Claude"

if [ $# -gt 1 ]; then
    echo "usage: $0 [<base>]" >&2
    exit 1
fi
# An empty argument is a caller whose "$base" did not get set, not a caller
# asking us to guess.  Guessing there hands filter-repo --force a range that
# can reach back past the real base into history that is not yours.
if [ $# -eq 1 ] && [ -z "$1" ]; then
    echo "!! base given but empty -- pass a commit, or no argument at all to guess" >&2
    exit 1
fi
if [ ! -f "$FILTER" ]; then
    echo "!! $FILTER is missing" >&2
    exit 1
fi
# filter-repo rewrites what the refs point at.  On a detached HEAD there is no
# branch to update, so it reports "New history written", exits 0, and the next
# checkout throws the rewrite away -- a silent no-op, which is the one outcome
# every other guard here exists to prevent.
if ! git symbolic-ref -q HEAD >/dev/null; then
    echo "!! HEAD is detached -- check out the branch you want rewritten first," >&2
    echo "   or the rewrite lands on nothing and is lost at the next checkout" >&2
    exit 1
fi
# Run through the interpreter rather than the shebang: a checkout on a
# filesystem that drops the mode bit (or core.fileMode=false) would otherwise
# silently cost every branch in a squash-magic run its co-author trailer.
export ADD_COAUTHOR_FILTER="$FILTER"

if [ $# -eq 1 ]; then
    if ! BASE_COMMIT=$(git rev-parse --verify --quiet "${1}^{commit}"); then
        echo "!! '$1' is not a commit" >&2
        exit 1
    fi
    # ...and reachable from HEAD.  "$BASE..HEAD" with an unrelated commit is not
    # an error to git, it is a much larger range -- everything in HEAD that the
    # base cannot reach, which for a sibling branch or a mistyped sha is most of
    # the history, and filter-repo --force will not object on your behalf.
    if ! git merge-base --is-ancestor "$BASE_COMMIT" HEAD; then
        echo "!! '$1' is not an ancestor of HEAD -- ${BASE_COMMIT:0:9}..HEAD would" >&2
        echo "   rewrite far more than this branch" >&2
        exit 1
    fi
    echo "Base commit is $BASE_COMMIT (given)"
else
    AUTHOR_NAME=$(git config user.name || true)
    if [ -z "$AUTHOR_NAME" ]; then
        echo "!! git config user.name is not set -- pass the base instead" >&2
        exit 1
    fi
    # The newest commit that is neither yours nor Claude's.  Both names are
    # compared whole -- Claude's allowing the model to follow it after a space,
    # since that name moves ("Claude", "Claude Opus 5 (1M context)").  Not a
    # substring test and not a bare prefix test: the first would swallow a
    # Jean-Claude, the second a Claudette, and either way the scan walks past a
    # real person's commit and filter-repo rewrites work that is not yours.
    BASE_COMMIT=$(git log --format="%H%x09%an" |
        awk -F'\t' -v me="$AUTHOR_NAME" -v alt="$ALT_AUTHOR_NAME" \
            '$2 != me && $2 != alt && index($2, alt " ") != 1 { print $1; exit }')
    if [ -z "$BASE_COMMIT" ]; then
        echo "!! no commit by anybody else to stop at -- pass the base instead" >&2
        exit 1
    fi
    echo "Base commit is $BASE_COMMIT (guessed: first commit not by $AUTHOR_NAME)"
fi

# The path comes through the environment, not spliced into this source: a
# checkout under a directory with an apostrophe in it would otherwise be a
# syntax error inside the callback, and worse in the general case.
git filter-repo --message-callback "
import os, subprocess, sys
return subprocess.run([sys.executable, os.environ['ADD_COAUTHOR_FILTER']],
                      input=message, stdout=subprocess.PIPE, check=True).stdout
" --refs "$BASE_COMMIT..HEAD" --force
