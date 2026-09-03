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
if [ ! -x "$FILTER" ]; then
    echo "!! $FILTER is missing or not executable" >&2
    exit 1
fi

if [ -n "${1:-}" ]; then
    if ! BASE_COMMIT=$(git rev-parse --verify --quiet "${1}^{commit}"); then
        echo "!! '$1' is not a commit" >&2
        exit 1
    fi
    echo "Base commit is $BASE_COMMIT (given)"
else
    AUTHOR_NAME=$(git config user.name || true)
    if [ -z "$AUTHOR_NAME" ]; then
        echo "!! git config user.name is not set -- pass the base instead" >&2
        exit 1
    fi
    # The newest commit that is neither yours nor Claude's.  Compared field by
    # field, so a name with regex characters in it (or one that is a substring
    # of somebody else's) still matches only itself.
    BASE_COMMIT=$(git log --format="%H%x09%an" |
        awk -F'\t' -v me="$AUTHOR_NAME" -v alt="$ALT_AUTHOR_NAME" \
            '$2 != me && $2 != alt { print $1; exit }')
    if [ -z "$BASE_COMMIT" ]; then
        echo "!! no commit by anybody else to stop at -- pass the base instead" >&2
        exit 1
    fi
    echo "Base commit is $BASE_COMMIT (guessed: first commit not by $AUTHOR_NAME)"
fi

git filter-repo --message-callback "
import subprocess
return subprocess.run(['$FILTER'], input=message,
                      stdout=subprocess.PIPE, check=True).stdout
" --refs "$BASE_COMMIT..HEAD" --force
