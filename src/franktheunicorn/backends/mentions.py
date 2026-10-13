"""Live ``@``-mentions in text frank is about to post, and how to defuse them.

Submitting a review already notifies the author and every subscriber. A
live ``@handle`` additionally summons the named person, without their
consent, onto a thread where nothing is being asked of them; and the text
frank posts is partly a model's and partly quoted from the PR, so a handle
in it was not the operator's decision. The rule, from Apache Magpie's
code-review skill (``plugins/magpie-pr-management/skills/code-review/
posting.md`` at apache/magpie commit dadb315caa8b881fc1d2195bef03223dac25915a):
nothing posted ever live-mentions anyone; a handle that must appear is
rendered backtick-quoted, which reads as a handle and notifies nobody.

``escape_handles`` and ``mention_scan`` are ported from the same commit's
``tools/pr-management/src/pr_management/code_review/body.py`` (Apache-2.0),
with one change: Magpie skips any line that starts with ``@word`` as a
Python decorator, which also skips a bare ``@bob`` alone on a line, so that
mention went out live. Here a decorator needs a dot or a parenthesis
(``@property`` on its own line outside a code fence is escaped, which is
harmless; ``@bob`` is escaped, which is the point). Code fences and inline
code spans are left alone in both.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_MENTION = re.compile(r"(?<![\w`@./])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:/[A-Za-z0-9_.-]+)?)\b")
_FENCE = re.compile(r"^\s*(```|~~~)")
#: GitHub treats ``@daily`` and friends as cron aliases, not mentions.
_CRON = frozenset(
    {"yearly", "annually", "monthly", "weekly", "daily", "hourly", "midnight", "once", "reboot"}
)
#: A decorator line: ``@name(`` or ``@pkg.name``; a bare ``@word`` is a mention.
_DECORATOR = re.compile(r"^\s*@[A-Za-z_]\w*(?:\.[\w.]+|\()")


@dataclass(frozen=True)
class MentionHit:
    handle: str
    line: int
    text: str


def normalize_allowlist(handles: Iterable[str]) -> frozenset[str]:
    """Lower-cased handles without a leading ``@``; what ``allowed`` wants."""
    return frozenset(h.strip().lstrip("@").lower() for h in handles if h and h.strip())


def escape_handles(text: str, allowed: frozenset[str] = frozenset()) -> str:
    """Backtick every live ``@handle`` outside code, so posting it notifies nobody."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if _FENCE.match(line):
            inside = not inside
            out.append(line)
            continue
        if inside or _DECORATOR.match(line):
            out.append(line)
            continue
        pieces = re.split(r"(`[^`]*`)", line)
        out.append(
            "".join(
                p
                if p.startswith("`")
                else _MENTION.sub(
                    lambda m: (
                        m.group(0)
                        if m.group(1).lower() in allowed or m.group(1).lower() in _CRON
                        else f"`@{m.group(1)}`"
                    ),
                    p,
                )
                for p in pieces
            )
        )
    return "\n".join(out)


def mention_scan(text: str, allowed: frozenset[str] = frozenset()) -> list[MentionHit]:
    """Live ``@``-mentions outside code spans, fences, decorators and cron aliases."""
    hits: list[MentionHit] = []
    inside = False
    for number, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            inside = not inside
            continue
        if inside or _DECORATOR.match(line):
            continue
        prose = re.sub(r"`[^`]*`", "", line)
        for match in _MENTION.finditer(prose):
            handle = match.group(1)
            if handle.lower() in _CRON or handle.lower() in allowed:
                continue
            hits.append(MentionHit(handle=handle, line=number, text=line.strip()))
    return hits
