#!/bin/bash
set -euo pipefail

# Claude Code SessionStart hook: prepare a Claude Code on the web session so
# tests, linters, and the pre-commit/pre-push gate (pre-push-lint.sh) work.
# Local development keeps using `make setup` — this exits immediately there.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
    exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"

# pyproject.toml requires Python >= 3.12 and CI runs 3.12, but the container
# default python3 may be older — pick a suitable interpreter explicitly.
PYTHON=""
for candidate in python3.12 python3.13 python3.14 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "session-start: no Python >= 3.12 interpreter found" >&2
    exit 1
fi

# Container state is cached between sessions, so reuse an existing .venv —
# but rebuild it if a stale one was created with a too-old interpreter.
if [ -e .venv/bin/python ] &&
    ! .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    rm -rf .venv
fi
if [ ! -e .venv/bin/python ]; then
    "$PYTHON" -m venv .venv
fi

.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[dev]"

# Put the venv first on PATH for the whole session so plain `pytest`, `ruff`,
# and `mypy` invocations resolve to the project environment.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    {
        echo "export VIRTUAL_ENV=\"$PWD/.venv\""
        echo "export PATH=\"$PWD/.venv/bin:\$PATH\""
    } >>"$CLAUDE_ENV_FILE"
fi

echo "session-start: $(.venv/bin/python --version) venv ready, dev dependencies installed" >&2
