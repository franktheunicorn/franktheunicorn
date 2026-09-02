#!/usr/bin/env bash
# update-bases.sh — Fast-forward local base branches from apache-github.
#
# Touches every local branch whose name is "master" or matches "branch-N.M"
# or "branch-N.x" AND whose version is >= $MIN_VERSION (default: 3.5), and
# fast-forwards it to $REMOTE/<name> — but only when the local ref is a
# strict ancestor of the upstream. Diverged branches are reported and left
# alone; missing upstreams are skipped.
#
# Env:
#   REMOTE       default: apache-github
#   MIN_VERSION  default: 3.5
#   NO_FETCH     default: unset (fetch is performed)

set -euo pipefail

REMOTE="${REMOTE:-apache-github}"
MIN_VERSION="${MIN_VERSION:-3.5}"

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  echo "!! remote '$REMOTE' is not configured" >&2
  exit 1
fi
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "!! working tree not clean — commit or stash tracked changes first" >&2
  exit 1
fi

if [ "${NO_FETCH:-0}" != "1" ]; then
  echo "==> git fetch --prune $REMOTE"
  git fetch --quiet --prune "$REMOTE"
fi

# Return 0 if version $1 >= version $2. Treats ".x" as very-late (.999).
version_ge() {
  local a="${1//.x/.999}" b="${2//.x/.999}"
  [ "$(printf '%s\n%s\n' "$a" "$b" | sort -V | tail -1)" = "$a" ]
}

ORIG_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")

# Detach so we can update any local branch (including the currently-checked-out
# one) without a working-tree dance. Restore ORIG_BRANCH on exit.
cleanup() {
  if [ -n "$ORIG_BRANCH" ] && [ "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo)" != "$ORIG_BRANCH" ]; then
    git checkout --quiet "$ORIG_BRANCH" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
git checkout --quiet --detach HEAD >/dev/null 2>&1 || true

updated=0; already=0; diverged=0; missing=0; skipped=0

mapfile -t local_branches < <(git for-each-ref --format='%(refname:short)' refs/heads/)

for br in "${local_branches[@]}"; do
  # Only master and branch-N.M / branch-N.x
  if [ "$br" = "master" ]; then
    :
  elif [[ "$br" =~ ^branch-([0-9]+(\.[0-9]+|\.x))$ ]]; then
    version="${BASH_REMATCH[1]}"
    if ! version_ge "$version" "$MIN_VERSION"; then
      skipped=$((skipped+1))
      continue
    fi
  else
    continue
  fi

  upstream="${REMOTE}/${br}"
  if ! git rev-parse --verify --quiet "refs/remotes/$upstream" >/dev/null; then
    echo "-- $br: no $upstream ref, skipping"
    missing=$((missing+1))
    continue
  fi

  local_sha=$(git rev-parse "$br")
  up_sha=$(git rev-parse "$upstream")
  if [ "$local_sha" = "$up_sha" ]; then
    echo "== $br: already at $upstream"
    already=$((already+1))
    continue
  fi
  if ! git merge-base --is-ancestor "$br" "$upstream" 2>/dev/null; then
    echo "!! $br: has diverged from $upstream, leaving alone" >&2
    diverged=$((diverged+1))
    continue
  fi

  git update-ref "refs/heads/$br" "$up_sha"
  n=$(git rev-list --count "${local_sha}..${up_sha}")
  echo "++ $br: fast-forwarded $n commit(s) from $upstream"
  updated=$((updated+1))
done

echo
echo "updated: $updated   already-current: $already   diverged: $diverged   missing-upstream: $missing   below-min: $skipped"
