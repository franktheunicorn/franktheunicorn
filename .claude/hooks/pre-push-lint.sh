#!/usr/bin/env bash
set -uo pipefail

# Claude Code PreToolUse hook: runs CI checks before any git push.
# Receives tool_input JSON on stdin. Exits 0 to allow, 2 to block.

COMMAND=$(python3 -c "import sys, json; print(json.load(sys.stdin).get('tool_input', {}).get('command', ''))")

# Only gate git push commands — let everything else through immediately.
if ! echo "$COMMAND" | grep -qE '^\s*git\s+push\b'; then
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

echo "Running pre-push checks (ruff, mypy, pytest)..." >&2

# 1. Ruff lint
RESULT=$(ruff check src/ tests/ 2>&1) || {
    FAILED=1
    OUTPUT+="=== ruff check FAILED ===
$RESULT

"
}

# 2. Ruff format
RESULT=$(ruff format --check src/ tests/ 2>&1) || {
    FAILED=1
    OUTPUT+="=== ruff format --check FAILED ===
$RESULT

"
}

# 3. mypy
RESULT=$(mypy src/franktheunicorn/ 2>&1) || {
    FAILED=1
    OUTPUT+="=== mypy FAILED ===
$RESULT

"
}

# 4. pytest (no coverage gating — that's CI's job)
RESULT=$(pytest -x --tb=short 2>&1) || {
    FAILED=1
    OUTPUT+="=== pytest FAILED ===
$RESULT

"
}

if [ "$FAILED" -ne 0 ]; then
    echo "Pre-push checks FAILED. Fix these issues before pushing:"
    echo ""
    echo "$OUTPUT"
    exit 2
fi

echo "All pre-push checks passed." >&2
exit 0
