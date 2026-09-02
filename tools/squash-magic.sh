#!/usr/bin/env bash
# squash-magic.sh
#
# For each input branch:
#   1. Fetch origin (unless NO_FETCH=1) — once up front (with --prune to
#      refresh base refs) and again per-branch just before processing it, so a
#      branch that changes mid-run always gets the newest tip. Then source the
#      branch from origin/<branch> (falling back to local <branch> if missing).
#   2. Pick candidate bases in order:
#        a. If the branch name ends in a recognized version suffix
#           (-master, -3.5, -4.1, -4.x, -4x which aliases -4.x, etc.),
#           the corresponding base (master / branch-3.5 / branch-4.x / …)
#           goes first.
#        b. Then $BASE_BRANCH (default origin/master).
#        c. Then $FALLBACK_BASE (default origin/branch-3.5).
#      Try to rebase a temp copy onto each candidate in order; use the first
#      one that succeeds. If none work — or the rebase succeeds but the
#      branch has zero commits above the base (i.e., already merged) —
#      look for a pre-existing local <branch>-squashed or <branch>-aok
#      (probably the artifact of an earlier squash-magic run before the
#      upstream merge landed); prefer -squashed. If found, append it to
#      $OUTPUT_FILE and record a note in $IDK_FILE (default: idk.txt) so a
#      human can verify. If neither pre-existing variant is present, append
#      the branch to $UNKNOWN_FILE (default: unknown_source.txt).
#   3. On the rebased copy, count commits above the base by author:
#         - Any commits by others → bail; append the branch to $WTFBBQ_FILE
#           (default: wtfbbq.txt) and print a warning.
#         - >1 commits, all mine  → squash into one → <branch>-squashed.
#         - 1 commit, all mine    → pointer         → <branch>-aok.
#      (The "0 commits above base" case is handled up in step 2 via
#      handle_probably_merged, since the branch appears already merged.)
#   4. Ask the `claude` CLI for a vague, no-security commit message when squashing.
#      If `claude` isn't available (or returns nothing), prompt the human via /dev/tty.
#   5. Check out the resulting branch and run ./add_coauthor.sh.
#   6. Append the resulting branch name to $OUTPUT_FILE (default: branches_to_merge.txt).
#
# The original local branches are NEVER modified — only new refs are created.
#
# Usage:
#   ./squash-magic.sh branch1 branch2 ...
#   ./squash-magic.sh -f branch_list.txt
#
# Env vars:
#   BASE_BRANCH     default: master           (local; fast-forwarded from its
#                                              tracking upstream — e.g. from
#                                              apache-github/master — before use)
#   FALLBACK_BASE   default: branch-3.5       (same auto-FF treatment)
#   REMOTE          default: origin
#   NO_FETCH        default: unset (fetch is performed)
#   ME_NAME         default: `git config user.name`, else "Holden Karau"
#   ME_EMAIL        default: `git config user.email`, else "holden@pigscanfly.ca"
#   OUTPUT_FILE     default: branches_to_merge.txt
#   UNKNOWN_FILE    default: unknown_source.txt
#   WTFBBQ_FILE     default: wtfbbq.txt
#   IDK_FILE        default: idk.txt
#   COAUTHOR_SCRIPT default: ./add_coauthor.sh

set -euo pipefail

REMOTE="${REMOTE:-origin}"
YIELD_SECONDS="${YIELD_SECONDS:-0}"
SYNC_BETWEEN_OPS="${SYNC_BETWEEN_OPS:-0}"
LOCK_WAIT_SECONDS="${LOCK_WAIT_SECONDS:-30}"
LOCK_RETRIES="${LOCK_RETRIES:-3}"
# Default to LOCAL base branches (which track whatever remote is authoritative
# for master/branch-3.5, e.g. apache-github/master). Using origin/<base> can be
# wrong when someone else's fork/mirror is the source of truth for the base.
BASE_BRANCH="${BASE_BRANCH:-master}"
FALLBACK_BASE="${FALLBACK_BASE:-branch-3.5}"
ME_NAME="${ME_NAME:-$(git config user.name 2>/dev/null || true)}"
ME_NAME="${ME_NAME:-Holden Karau}"
ME_EMAIL="${ME_EMAIL:-$(git config user.email 2>/dev/null || true)}"
ME_EMAIL="${ME_EMAIL:-holden@pigscanfly.ca}"
OUTPUT_FILE="${OUTPUT_FILE:-branches_to_merge.txt}"
UNKNOWN_FILE="${UNKNOWN_FILE:-unknown_source.txt}"
WTFBBQ_FILE="${WTFBBQ_FILE:-wtfbbq.txt}"
IDK_FILE="${IDK_FILE:-idk.txt}"
COAUTHOR_SCRIPT="${COAUTHOR_SCRIPT:-./add_coauthor.sh}"

usage() {
  cat >&2 <<EOF
Usage:
  $0 branch1 branch2 ...
  $0 -f branch_list.txt

Environment:
  BASE_BRANCH     default: $BASE_BRANCH
  FALLBACK_BASE   default: $FALLBACK_BASE
  REMOTE          default: $REMOTE
  NO_FETCH        default: (fetch enabled)
  ME_NAME         default: $ME_NAME
  ME_EMAIL        default: $ME_EMAIL
  OUTPUT_FILE     default: $OUTPUT_FILE
  UNKNOWN_FILE    default: $UNKNOWN_FILE
  WTFBBQ_FILE     default: $WTFBBQ_FILE
  IDK_FILE        default: $IDK_FILE
  COAUTHOR_SCRIPT default: $COAUTHOR_SCRIPT
EOF
  exit 1
}

[ $# -gt 0 ] || usage

if [ "$1" = "-f" ]; then
  [ -n "${2:-}" ] || usage
  mapfile -t BRANCHES < <(sed -e 's/#.*//' "$2" | awk 'NF')
else
  BRANCHES=("$@")
fi

# ---- staying out of another git's way ---------------------------------------
# Every checkout, rebase and merge takes .git/index.lock, and git does not queue
# for it: if anything else in this repo holds it -- another script, an editor, a
# plain `git status` in a second shell -- the command fails outright.  A rebase
# that fails that way looks exactly like a rebase that hit a conflict, and
# reading it as one sends the branch to the wrong base (or to
# $UNKNOWN_FILE).  So: yield between operations, wait for a lock we can
# see, and when git says the lock was the problem, go again rather than believe
# it.
GIT_DIR_PATH=$(git rev-parse --absolute-git-dir)

# Hand the CPU over before the next git command.  Zero seconds by default: the
# fork is the yield, and that is all this needs to be.
settle() {
  sleep "$YIELD_SECONDS"
  [ "$SYNC_BETWEEN_OPS" = "1" ] && sync
  return 0
}

# Wait while somebody else's lock is on the index.  Not an error if it outstays
# $LOCK_WAIT_SECONDS -- git is about to say so far more precisely than we can.
wait_for_index_lock() {
  local waited=0
  while [ -e "$GIT_DIR_PATH/index.lock" ] && [ "$waited" -lt "$LOCK_WAIT_SECONDS" ]; do
    [ "$waited" = 0 ] && echo "    (another git holds index.lock; waiting)" >&2
    sleep 1
    waited=$((waited + 1))
  done
  return 0
}

# Did this command fail over a lock rather than over the work it was asked to do?
lock_error() {
  grep -qE "index\.lock|Another git process|cannot lock ref|Unable to create.*\.lock" "$1"
}

# git, after letting anyone else finish.  For anything that takes the index lock.
git_s() {
  settle
  wait_for_index_lock
  git "$@"
}

# Sanity: remote, clean working tree.
if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  echo "!! remote '$REMOTE' is not configured" >&2
  exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "!! working tree not clean — commit or stash tracked changes first" >&2
  exit 1
fi

# Fetch ALL remotes unless suppressed, so local base branches (master,
# branch-3.5, etc.) can be fast-forwarded from whichever upstream they track.
if [ "${NO_FETCH:-0}" != "1" ]; then
  echo "==> git fetch --all --prune"
  git fetch --quiet --all --prune
fi

if ! git rev-parse --verify --quiet "$BASE_BRANCH" >/dev/null; then
  echo "!! base '$BASE_BRANCH' does not exist" >&2
  exit 1
fi
if [ -n "$FALLBACK_BASE" ] && ! git rev-parse --verify --quiet "$FALLBACK_BASE" >/dev/null; then
  echo "!! fallback base '$FALLBACK_BASE' does not exist — will only try $BASE_BRANCH" >&2
  FALLBACK_BASE=""
fi

ORIG_BRANCH=$(git rev-parse --abbrev-ref HEAD)
: > "$OUTPUT_FILE"
: > "$UNKNOWN_FILE"
: > "$WTFBBQ_FILE"
: > "$IDK_FILE"

WORK_PREFIX="_squash_magic_tmp"
ERR_FILE=$(mktemp)

cleanup() {
  rm -f "$ERR_FILE"
  git rebase --abort >/dev/null 2>&1 || true
  local cur
  cur=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
  if [ "$cur" != "$ORIG_BRANCH" ]; then
    git checkout --quiet "$ORIG_BRANCH" >/dev/null 2>&1 || true
  fi
  while IFS= read -r b; do
    [ -n "$b" ] && git branch -D "$b" >/dev/null 2>&1 || true
  done < <(git for-each-ref --format='%(refname:short)' "refs/heads/${WORK_PREFIX}_*" 2>/dev/null || true)
}
trap cleanup EXIT

# Detach HEAD so we can freely force-update any branch.
git_s checkout --quiet --detach HEAD >/dev/null 2>&1 || true

# Bookkeeping markers a rewrite carries.  Taken off before the name is read for
# anything else, the same as merge_branches.py's STRIP_SUFFIXES.
STRIP_SUFFIXES=("-aok" "-squashed")

# The branch name with those markers off: f11-parquet-eager-alloc-4.1-aok is
# f11-parquet-eager-alloc-4.1, and the version is then there to be found.
logical_name() {
  local br="$1" suffix changed=1
  while [ "$changed" = "1" ]; do
    changed=0
    for suffix in "${STRIP_SUFFIXES[@]}"; do
      if [ "$br" != "$suffix" ] && [ "${br%"$suffix"}" != "$br" ]; then
        br="${br%"$suffix"}"
        changed=1
      fi
    done
  done
  printf '%s' "$br"
}

# If $1 ends in a recognized version suffix, print the derived base ref (preferring
# the remote-tracking version) and return 0. Otherwise return 1.
#
# The pin is written several ways for the same release, and this reads all of
# them the way merge_branches.py's VERSION_SUFFIX does, so the two scripts never
# disagree about where a branch was cut from:
#   -master        → master
#   -3.5           → branch-3.5
#   -4.1           → branch-4.1
#   -4.x           → branch-4.x
#   -4x            → branch-4.x        (alias for -4.x)
#   -branch-4.x    → branch-4.x        ("-branch" belongs to the version)
#   -4.x-r2        → branch-4.x        (-rN marks a redone cut, not a version)
#   -branch-4.2-aok → branch-4.2       (markers come off first)
#   -N.M.P...      → branch-N.M.P...
derive_base_from_suffix() {
  local br suffix base
  br=$(logical_name "$1")
  if [[ "$br" =~ -(branch-)?(master|[0-9]+(\.[0-9]+|\.x)+|[0-9]+x)(-r[0-9]+)?$ ]]; then
    suffix="${BASH_REMATCH[2]}"
    # Normalize -Nx → -N.x
    if [[ "$suffix" =~ ^([0-9]+)x$ ]]; then
      suffix="${BASH_REMATCH[1]}.x"
    fi
    if [ "$suffix" = "master" ]; then
      base="master"
    else
      base="branch-$suffix"
    fi
    # Prefer LOCAL branch (which tracks the authoritative upstream); fall back
    # to $REMOTE/<base> only if there is no local ref.
    if git rev-parse --verify --quiet "refs/heads/${base}" >/dev/null; then
      printf '%s' "$base"
      return 0
    elif git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${base}" >/dev/null; then
      printf '%s' "${REMOTE}/${base}"
      return 0
    else
      echo "    (suffix implies '$base' but it doesn't exist; will use defaults)" >&2
      return 1
    fi
  fi
  return 1
}

# Fast-forward a local branch from its tracking upstream, but only if the local
# branch is a strict ancestor of the upstream (i.e. safe / no rewrite). No-op
# if the ref is a remote-tracking ref, has no upstream, or has diverged.
ff_from_upstream() {
  local br="$1" up up_sha br_sha cur
  # Only local branches (no "/" or, more precisely, an entry under refs/heads).
  git rev-parse --verify --quiet "refs/heads/$br" >/dev/null || return 0
  up=$(git rev-parse --abbrev-ref --symbolic-full-name "${br}@{upstream}" 2>/dev/null || true)
  [ -n "$up" ] || return 0
  git merge-base --is-ancestor "$br" "$up" 2>/dev/null || return 0
  br_sha=$(git rev-parse "$br")
  up_sha=$(git rev-parse "$up")
  [ "$br_sha" = "$up_sha" ] && return 0
  cur=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
  if [ "$cur" = "$br" ]; then
    git_s merge --ff-only --quiet "$up" 2>/dev/null || true
  else
    git update-ref "refs/heads/$br" "$up_sha" 2>/dev/null || true
  fi
}

# Try rebasing $1 (a branch name) onto $2. Returns 0 on clean rebase.
#
# Only a real conflict returns 1.  A checkout or rebase that fell over because
# another git in this repo held the index lock is tried again -- up to
# $LOCK_RETRIES times -- because believing that one would send the branch on to
# the next candidate base and quietly cut the rewrite from the wrong place.
try_rebase() {
  local br="$1" base="$2" attempt=1
  while :; do
    if ! git_s checkout --quiet "$br" 2>"$ERR_FILE"; then
      if lock_error "$ERR_FILE" && [ "$attempt" -lt "$LOCK_RETRIES" ]; then
        echo "    (lost the lock race checking out $br; going again)" >&2
        sleep "$attempt"; attempt=$((attempt + 1)); continue
      fi
      # Not a conflict and not a lock: usually untracked files left in the
      # working tree that the target branch tracks.  Say which -- the operator
      # has to clear them, this script will not delete anything.
      echo "    !! cannot check out $br:" >&2
      sed -n '1,8p' "$ERR_FILE" | sed 's/^/       /' >&2
      return 1
    fi
    if git_s rebase --quiet "$base" >/dev/null 2>"$ERR_FILE"; then
      return 0
    fi
    git rebase --abort >/dev/null 2>&1 || true
    if lock_error "$ERR_FILE" && [ "$attempt" -lt "$LOCK_RETRIES" ]; then
      echo "    (lost the lock race rebasing onto $base; going again)" >&2
      sleep "$attempt"; attempt=$((attempt + 1)); continue
    fi
    return 1
  done
}

# Get a commit message (claude if available, else prompt human via /dev/tty).
get_commit_message() {
  local diff_payload="$1" msg=""

  if command -v claude >/dev/null 2>&1; then
    msg=$(printf '%s' "$diff_payload" | claude -p 'Write a short, deliberately vague and obtuse git commit message summarizing these diff changes. Keep it under 8 lines total (subject line, then blank line, then optional brief body). Do NOT mention security, CVEs, vulnerabilities, hardening, sanitization, validation, escaping, injection, exploits, permissions, or any security-related terminology. Prefer generic phrases like "refinements", "adjustments", "cleanup", "polish", "tidying", "internal changes". Output only the commit message text — no preamble, no code fences, no explanation.' 2>/dev/null || true)
    msg="${msg//$'\r'/}"
    msg=$(printf '%s' "$msg" | sed -e '/^```/d' -e '/./,$!d')
  fi

  if [ -z "$msg" ]; then
    {
      echo ""
      echo "!! claude CLI unavailable or returned nothing — please supply a commit message."
      echo "   Files changed:"
      printf '%s\n' "$diff_payload" | awk '/^=== diff/{exit} {print "     " $0}'
      echo "   Enter commit message below. End input with a single '.' on its own line, or Ctrl-D:"
    } >&2
    if [ -r /dev/tty ]; then
      msg=$(awk '/^\.$/{exit} {print}' </dev/tty)
    else
      msg=$(awk '/^\.$/{exit} {print}')
    fi
    msg=$(printf '%s' "$msg" | sed -e '/./,$!d')
  fi

  [ -n "$msg" ] || msg="Assorted refinements and light housekeeping"
  printf '%s' "$msg"
}

# Called when we can't produce a fresh -squashed/-aok for a branch (either no
# clean base found, or rebase produced zero commits above the base). Looks for
# a pre-existing <branch>-squashed / -aok, prefers -squashed, and if one is
# present appends it to $OUTPUT_FILE and a note to $IDK_FILE for human review.
# Otherwise appends the branch name to $UNKNOWN_FILE.
# Args: $1 = branch name, $2 = short reason string for the log lines.
handle_probably_merged() {
  local br="$1" reason="$2"
  local sq="${br}-squashed" ao="${br}-aok"
  local sq_exists=0 ao_exists=0 reuse="" warn=""
  git rev-parse --verify --quiet "refs/heads/$sq" >/dev/null && sq_exists=1
  git rev-parse --verify --quiet "refs/heads/$ao" >/dev/null && ao_exists=1

  if [ "$sq_exists" = "1" ]; then
    reuse="$sq"
    [ "$ao_exists" = "1" ] && warn=" (WARN: both -squashed and -aok exist; using -squashed)"
  elif [ "$ao_exists" = "1" ]; then
    reuse="$ao"
  fi

  if [ -n "$reuse" ]; then
    echo "$reuse" >> "$OUTPUT_FILE"
    echo "$br -> $reuse ($reason; likely already merged to master)$warn" >> "$IDK_FILE"
    echo "    -> $reuse (see $IDK_FILE: $reason${warn})"
  else
    echo "$br" >> "$UNKNOWN_FILE"
    echo "    -> $UNKNOWN_FILE ($reason)"
  fi
}

# Run ./add_coauthor.sh on the currently-checked-out branch.
run_add_coauthor() {
  if [ ! -x "$COAUTHOR_SCRIPT" ] && [ ! -f "$COAUTHOR_SCRIPT" ]; then
    echo "    (skipping add_coauthor: $COAUTHOR_SCRIPT not found)" >&2
    return 0
  fi
  echo "    running $COAUTHOR_SCRIPT ..."
  if [ -x "$COAUTHOR_SCRIPT" ]; then
    "$COAUTHOR_SCRIPT" >/dev/null 2>&1 || {
      echo "    !! $COAUTHOR_SCRIPT failed on $(git rev-parse --abbrev-ref HEAD)" >&2
      return 1
    }
  else
    bash "$COAUTHOR_SCRIPT" >/dev/null 2>&1 || {
      echo "    !! $COAUTHOR_SCRIPT failed on $(git rev-parse --abbrev-ref HEAD)" >&2
      return 1
    }
  fi
}

for branch in "${BRANCHES[@]}"; do
  echo "==> $branch"

  # Refresh this branch from origin every time, in case it moved since the
  # initial fetch (e.g., you pushed a new tip while the script was running).
  if [ "${NO_FETCH:-0}" != "1" ]; then
    git fetch --quiet "$REMOTE" "$branch" 2>/dev/null || true
  fi

  # Prefer origin/<branch>; fall back to local <branch>.
  if git rev-parse --verify --quiet "refs/remotes/${REMOTE}/${branch}" >/dev/null; then
    source_ref="${REMOTE}/${branch}"
  elif git rev-parse --verify --quiet "refs/heads/$branch" >/dev/null; then
    source_ref="$branch"
    echo "    (source: local $branch — not on $REMOTE)"
  else
    echo "    !! not on $REMOTE and no local branch, skipping" >&2
    continue
  fi

  work="${WORK_PREFIX}_${branch//\//__}"
  git_s checkout --quiet --detach HEAD >/dev/null 2>&1 || true
  git branch --no-track -f "$work" "$source_ref"

  # 1. Rebase probe against each candidate base.
  # Order: suffix-derived (if any) → BASE_BRANCH → FALLBACK_BASE, deduplicated.
  base=""
  raw_candidates=()
  if derived=$(derive_base_from_suffix "$branch"); then
    raw_candidates+=("$derived")
    echo "    suffix-derived base candidate: $derived"
  fi
  raw_candidates+=("$BASE_BRANCH")
  [ -n "$FALLBACK_BASE" ] && raw_candidates+=("$FALLBACK_BASE")

  unset seen_candidates || true
  declare -A seen_candidates
  candidates=()
  for c in "${raw_candidates[@]}"; do
    if [ -z "${seen_candidates[$c]:-}" ]; then
      # If this is a local branch that tracks an upstream, fast-forward it so
      # we rebase against the current tip (not a stale local snapshot).
      ff_from_upstream "$c"
      candidates+=("$c")
      seen_candidates[$c]=1
    fi
  done

  landed_in=""
  for candidate in "${candidates[@]}"; do
    git_s checkout --quiet --detach HEAD >/dev/null 2>&1 || true
    git branch --no-track -f "$work" "$source_ref"  # reset temp for each attempt
    if try_rebase "$work" "$candidate"; then
      base="$candidate"
      # Rebased clean and left nothing on top: this work is already in
      # $candidate, which is the answer and not a reason to keep looking.
      # Trying the next base would find one this landed change still applies
      # to and cut a rewrite against it -- a backport of something that is
      # already upstream.  Take the rewrite an earlier run made instead.
      if [ -z "$(git log --format=%H "${candidate}..${work}")" ]; then
        echo "    already landed in $candidate -- not trying the other bases"
        landed_in="$candidate"
      else
        echo "    rebased cleanly onto $candidate"
      fi
      break
    else
      echo "    rebase onto $candidate: conflict"
    fi
  done

  if [ -n "$landed_in" ]; then
    git_s checkout --quiet --detach HEAD >/dev/null 2>&1 || true
    git branch -D "$work" >/dev/null 2>&1 || true
    handle_probably_merged "$branch" "already landed in $landed_in"
    continue
  fi

  if [ -z "$base" ]; then
    git_s checkout --quiet --detach HEAD >/dev/null 2>&1 || true
    git branch -D "$work" >/dev/null 2>&1 || true
    handle_probably_merged "$branch" "no clean base found"
    continue
  fi

  # 2. Split commits above $base on the rebased $work into mine vs. others.
  mapfile -t commit_lines < <(git log --reverse --format='%H%x09%an%x09%ae' "$base..$work")

  if [ ${#commit_lines[@]} -eq 0 ]; then
    # Belt and braces: the loop above already stops on a base that swallowed
    # the whole branch, so this is only reached if the two disagreed.
    git_s checkout --quiet --detach HEAD >/dev/null 2>&1 || true
    git branch -D "$work" >/dev/null 2>&1 || true
    handle_probably_merged "$branch" "no commits above $base after rebase"
    continue
  fi

  mine=(); others=()
  for line in "${commit_lines[@]}"; do
    IFS=$'\t' read -r sha an ae <<<"$line"
    if [ "$an" = "$ME_NAME" ] || [ "$ae" = "$ME_EMAIL" ]; then
      mine+=("$sha")
    else
      others+=("$sha")
    fi
  done

  # 3a. If there are ANY commits from others, refuse to touch this branch.
  if [ ${#others[@]} -ge 1 ]; then
    git_s checkout --quiet --detach HEAD >/dev/null 2>&1 || true
    git branch -D "$work" >/dev/null 2>&1 || true
    echo "$branch" >> "$WTFBBQ_FILE"
    echo "    !! $branch has ${#others[@]} commit(s) from others (mine: ${#mine[@]}) — bailing" >&2
    echo "    -> $WTFBBQ_FILE"
    continue
  fi

  # 3b. All mine. If ≤1, no squashing needed — -aok points at the rebased tip.
  if [ ${#mine[@]} -le 1 ]; then
    target="${branch}-aok"
    git_s checkout --quiet --detach HEAD >/dev/null 2>&1 || true
    git branch -f "$target" "$work"
    git_s checkout --quiet "$target"
    run_add_coauthor || true
    git branch -D "$work" >/dev/null 2>&1 || true
    echo "$target" >> "$OUTPUT_FILE"
    echo "    -> $target (${#mine[@]} of mine, nothing to squash)"
    continue
  fi

  # 3c. >1 mine, 0 others — squash all mine commits into one on top of $base.
  target="${branch}-squashed"
  parent=$(git rev-parse "$base")
  target_tree=$(git rev-parse "${work}^{tree}")

  # Build the Claude-friendly payload from base..work.
  diff_payload=$({
    echo "=== files changed ==="
    git diff --stat "$parent" "$work" || true
    echo
    echo "=== diff (may be truncated) ==="
    git diff "$parent" "$work" || true
  } | head -c 200000)
  msg=$(get_commit_message "$diff_payload")

  new_commit=$(GIT_AUTHOR_NAME="$ME_NAME"  GIT_AUTHOR_EMAIL="$ME_EMAIL" \
               GIT_COMMITTER_NAME="$ME_NAME" GIT_COMMITTER_EMAIL="$ME_EMAIL" \
               git commit-tree "$target_tree" -p "$parent" -m "$msg")

  git_s checkout --quiet --detach HEAD >/dev/null 2>&1 || true
  git update-ref "refs/heads/$target" "$new_commit"
  git_s checkout --quiet "$target"

  run_add_coauthor || true
  git branch -D "$work" >/dev/null 2>&1 || true

  echo "$target" >> "$OUTPUT_FILE"
  echo "    -> $target (squashed ${#mine[@]} of mine into one commit on $base)"
done

git_s checkout --quiet "$ORIG_BRANCH"
trap - EXIT

nm=$(wc -l < "$OUTPUT_FILE"  | tr -d ' ')
nu=$(wc -l < "$UNKNOWN_FILE" | tr -d ' ')
nw=$(wc -l < "$WTFBBQ_FILE"  | tr -d ' ')
ni=$(wc -l < "$IDK_FILE"     | tr -d ' ')
echo
echo "Wrote $nm branch(es) to $OUTPUT_FILE"
echo "Wrote $nu branch(es) to $UNKNOWN_FILE"
echo "Wrote $nw branch(es) to $WTFBBQ_FILE"
echo "Wrote $ni line(s) to $IDK_FILE (branches needing human review)"
