#!/usr/bin/env bash
set -uo pipefail

# Claude Code PreToolUse hook: runs CI checks before any git push.
# Receives tool_input JSON on stdin. Exits 0 to allow, 2 to block.

HOOK_INPUT=$(cat)

# Determine which gate we're in: commit, push, or neither. git_gate.py splits the
# command into simple commands the way a shell does, so `cd repo && git push`,
# `git -C repo push` and a push inside a retry loop are all gated; an anchored
# regex let every one of those through. If the helper cannot run, fail closed
# on any command that mentions git at all.
GATE=$(printf '%s' "$HOOK_INPUT" | python3 "$(dirname "$0")/git_gate.py" 2>/dev/null) || GATE=""
if [ -z "$GATE" ]; then
    if printf '%s' "$HOOK_INPUT" | grep -q 'git.*push'; then
        GATE=push
    elif printf '%s' "$HOOK_INPUT" | grep -q 'git.*commit'; then
        GATE=commit
    else
        GATE=none
    fi
fi
IS_COMMIT=0
IS_PUSH=0
if [ "$GATE" = "push" ]; then
    IS_PUSH=1
elif [ "$GATE" = "commit" ]; then
    IS_COMMIT=1
fi

# Let non-git-commit/push commands through immediately.
if [ "$IS_COMMIT" -eq 0 ] && [ "$IS_PUSH" -eq 0 ]; then
    exit 0
fi

# Navigate to repo root (this script lives at .claude/hooks/)
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# Activate venv if present
if [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

FAILED=0
OUTPUT=""

# Use python -m to ensure tools run in the project's Python environment
# (bare mypy/pytest may resolve to uv-managed isolated installs that
# lack django-stubs and project deps).

if [ "$IS_COMMIT" -eq 1 ]; then
    echo "Running pre-commit checks (ruff, mypy)..." >&2
else
    echo "Running pre-push checks (ruff, mypy, pytest)..." >&2
fi

# 1. Ruff lint
RESULT=$(python -m ruff check src/ tests/ 2>&1) || {
    FAILED=1
    OUTPUT+="=== ruff check FAILED ===
$RESULT

"
}

# 2. Ruff format
RESULT=$(python -m ruff format --check src/ tests/ 2>&1) || {
    FAILED=1
    OUTPUT+="=== ruff format --check FAILED ===
$RESULT

"
}

# 3. mypy
RESULT=$(python -m mypy src/franktheunicorn/ 2>&1) || {
    FAILED=1
    OUTPUT+="=== mypy FAILED ===
$RESULT

"
}

# 4. pytest — only on push (too slow for every commit)
if [ "$IS_PUSH" -eq 1 ]; then
    RESULT=$(python -m pytest -x --tb=short 2>&1) || {
        FAILED=1
        OUTPUT+="=== pytest FAILED ===
$RESULT

"
    }
fi

if [ "$FAILED" -ne 0 ]; then
    if [ "$IS_COMMIT" -eq 1 ]; then
        echo "Pre-commit checks FAILED. Fix these issues before committing:"
    else
        echo "Pre-push checks FAILED. Fix these issues before pushing:"
    fi
    echo ""
    echo "$OUTPUT"
    exit 2
fi

if [ "$IS_COMMIT" -eq 1 ]; then
    echo "All pre-commit checks passed." >&2
else
    echo "All pre-push checks passed." >&2
fi
exit 0
