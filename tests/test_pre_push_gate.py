"""The pre-push hook's gate: which Bash commands cross ``git push`` / ``git commit``.

The hook used anchored regexes, so every one of the "push" cases below except
the first walked past the full test run. Loaded from ``.claude/hooks`` by path:
it is developer tooling, not part of the installed package.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_HOOK_DIR = Path(__file__).resolve().parents[1] / ".claude" / "hooks"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("git_gate", _HOOK_DIR / "git_gate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load()


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin main",
        "cd /home/user/repo && git push -u origin main",
        "git -C /home/user/repo push",
        "/usr/bin/git push origin HEAD",
        "git --no-pager -c core.editor=true push --force-with-lease",
        "for i in 1 2 3; do if git push -u origin b 2>&1; then echo ok; break; fi; sleep 2; done",
        "GIT_TRACE=1 git push",
        "timeout 600 git push",
        "cd repo\ngit push",
        "git commit -q -m 'x' && git push",
        # An apostrophe in a comment must not open a quote over the next line.
        "cd repo\n# Push the branch (it's already committed)\ngit push -u origin b\n# it's done",
        "git commit -m wip # it's a wip\ngit push # it's fine",
        # ``)`` glued to an operator still ends the command before it.
        'BR=$(git rev-parse --abbrev-ref HEAD); git push -u origin "$BR"',
        "(cd sub && make)&& git push",
        "echo $(date)|| git push",
    ],
)
def test_push_is_gated_however_it_is_spelled(command: str) -> None:
    assert gate.gate_for(command) == "push"


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'fix'",
        "cd repo && git add -A && git commit -q -F - <<'EOF'\nfix: it's the parser's fault\n\nCo-Authored-By: x\nEOF",
        "git -c core.editor=true commit --amend --no-edit",
    ],
)
def test_commit_is_gated(command: str) -> None:
    assert gate.gate_for(command) == "commit"


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "git status --short",
        "git log --oneline -n 5",
        "echo 'git push is how you publish'",
        "grep -rn 'git push' docs/",
        "git commit-tree HEAD^{tree}",
        "cat <<'EOF'\ngit push origin main\nEOF",
        "# git push later\ngit status",
        'echo "not # a comment"; git log',
        "echo ${#arr}; git diff",
    ],
)
def test_everything_else_passes(command: str) -> None:
    assert gate.gate_for(command) == "none"


def test_an_untokenisable_command_fails_closed() -> None:
    assert gate.gate_for("git push 'unbalanced") == "push"
    assert gate.gate_for("echo 'unbalanced") == "none"


def test_the_script_reads_hook_json_from_stdin() -> None:
    payload = json.dumps({"tool_input": {"command": "cd x && git push"}})
    done = subprocess.run(
        [sys.executable, str(_HOOK_DIR / "git_gate.py")],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == "push"


def test_malformed_hook_input_is_not_a_gate() -> None:
    done = subprocess.run(
        [sys.executable, str(_HOOK_DIR / "git_gate.py")],
        input="not json",
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == "none"
