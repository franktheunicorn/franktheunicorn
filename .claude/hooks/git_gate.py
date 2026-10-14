"""Which pre-flight gate a Bash command crosses: ``push``, ``commit`` or ``none``.

Reads the PreToolUse hook JSON on stdin and prints one word. ``pre-push-lint.sh``
runs the fast checks for ``commit`` and the full suite for ``push``.

The gate used to be two regexes anchored at the start of the command
(``^\\s*git\\s+push``), so ``cd repo && git push``, ``git -C repo push``,
``/usr/bin/git push`` and a push inside a retry loop (``for i in 1 2 3; do if
git push ...``) all walked past it. That is how a failing test reached CI on
PR 136. This parses the command the way a shell would split it instead: into
simple commands at the control operators, leading keywords and ``NAME=value``
assignments dropped, the head normalised to its basename, and git's global
options skipped before the subcommand is read.

The segment split, the basename rule and the global-option table are ported
from Apache Magpie's agent-guard (``tools/agent-guard/src/agent_guard/__init__.py``,
``split_segments``, ``Segment`` and ``git_subcommand_index``, at apache/magpie
commit dadb315caa8b881fc1d2195bef03223dac25915a, Apache-2.0). Two additions
here: heredoc bodies are dropped before lexing (a commit message with an
apostrophe would otherwise fail to tokenise), and a command that still cannot
be tokenised falls back to a plain text search, failing closed: one extra lint
run costs less than a skipped gate.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

#: Characters shlex groups into operator tokens. A token made only of these
#: is a separator, whatever the run: ``)`` glued to ``;`` or ``&&`` lexes as
#: one token (``);``, ``)&&``) and must still end the segment before it.
_OPERATOR_CHARS = frozenset(";&|()")

#: Words that can precede the command proper in a segment.
_PREFIX_WORDS = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "time",
     "command", "exec", "builtin", "nohup", "env"}
)

#: git global options that take a value as the next token (Magpie's table).
_GIT_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--exec-path", "--html-path", "--man-path", "--info-path",
     "--namespace", "--work-tree", "--git-dir", "--super-prefix", "--config-env"}
)

_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_comments(command: str) -> str:
    """*command* with shell comments removed, quote-aware.

    A ``#`` starts a comment only at the start of a word and outside quotes,
    and the comment runs to the end of its line. Without this, an apostrophe
    in a comment ("# it's committed") opened a quote that swallowed the next
    line's ``git push``, and the tokeniser still balanced it.
    """
    out: list[str] = []
    quote = ""
    prev = "\n"
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            out.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                out.append(command[i + 1])
                i += 2
                prev = command[i - 1]
                continue
            if ch == quote:
                quote = ""
        elif ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(command[i + 1])
            i += 2
            prev = command[i - 1]
            continue
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "#" and (prev.isspace() or prev in _OPERATOR_CHARS):
            end = command.find("\n", i)
            if end == -1:
                break
            i = end
            continue
        else:
            out.append(ch)
        prev = ch
        i += 1
    return "".join(out)


def strip_heredocs(command: str) -> str:
    """*command* with every heredoc body removed (the ``<<EOF`` line stays)."""
    lines = command.split("\n")
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for match in _HEREDOC.finditer(line):
            terminator = match.group(2)
            while i < len(lines) and lines[i].strip() != terminator:
                i += 1
            i += 1  # the terminator line itself
    return "\n".join(kept)


def segments(command: str) -> list[list[str]]:
    """Simple commands in *command*, as argv lists. Raises ValueError when the
    text cannot be tokenised (unbalanced quotes)."""
    lexer = shlex.shlex(
        strip_comments(strip_heredocs(command)).replace("\n", " ; "),
        posix=True,
        punctuation_chars=";&|()",
    )
    lexer.whitespace_split = True
    lexer.commenters = ""
    out: list[list[str]] = []
    current: list[str] = []
    for token in lexer:
        if token and set(token) <= _OPERATOR_CHARS:
            if current:
                out.append(current)
            current = []
        else:
            current.append(token)
    if current:
        out.append(current)
    return out


def command_argv(segment: list[str]) -> list[str]:
    """The segment with keywords, wrappers and env assignments peeled off and
    the head reduced to its basename (``/usr/bin/git`` is ``git``)."""
    i = 0
    while i < len(segment):
        token = segment[i]
        if token in _PREFIX_WORDS or _ASSIGNMENT.fullmatch(token):
            i += 1
        elif token == "timeout":
            i += 1
            while i < len(segment) and (segment[i].startswith("-") or segment[i][:1].isdigit()):
                i += 1
        else:
            break
    argv = segment[i:]
    if argv:
        argv = [os.path.basename(argv[0]), *argv[1:]]
    return argv


def git_subcommand(argv: list[str]) -> str | None:
    """``push`` for ``git -C repo push``; None when *argv* is not a git call."""
    if not argv or argv[0] != "git":
        return None
    i = 1
    while i < len(argv):
        token = argv[i]
        if token.startswith("-") and token != "-":
            i += 2 if ("=" not in token and token in _GIT_VALUE_FLAGS) else 1
            continue
        return token
    return None


def gate_for(command: str) -> str:
    """``push`` if any simple command is a git push, else ``commit`` if any is a
    git commit, else ``none``."""
    try:
        subcommands = {git_subcommand(command_argv(seg)) for seg in segments(command)}
    except ValueError:
        text = strip_comments(strip_heredocs(command))
        if re.search(r"\bgit\b[^\n]*\bpush\b", text):
            return "push"
        if re.search(r"\bgit\b[^\n]*\bcommit\b", text):
            return "commit"
        return "none"
    if "push" in subcommands:
        return "push"
    if "commit" in subcommands:
        return "commit"
    return "none"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        command = str(payload.get("tool_input", {}).get("command", ""))
    except (ValueError, AttributeError):
        print("none")
        return 0
    print(gate_for(command))
    return 0


if __name__ == "__main__":
    sys.exit(main())
