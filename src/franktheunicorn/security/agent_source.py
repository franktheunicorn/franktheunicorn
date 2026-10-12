"""Which agent a report came from, and scoping work to one.

Another agent's output is a different kind of input from a person's report, and
the backlog has to be able to say so. A scanner archive is a hundred findings
nobody has looked at, one per call site, with severities the scanner assigned
itself; an emailed report is one person who thought it worth writing up. The
operator wants to read the two differently, ask "how often has agent X been
right?", and spend money on one without spending it on the other.

So every report carries ``source_agent`` (blank for a human), and everything that
selects reports in bulk — the list, its tab counts, the CSV export and every bulk
button — takes an *agent scope*:

* ``all`` — no filter, and the default everywhere, so nothing that predates this
  changes behaviour;
* ``none`` — only reports no agent produced;
* ``any`` — every agent-produced report;
* anything else — that one agent's reports.

The three keywords are reserved: :func:`clean_agent_name` refuses them as names,
so a scope can never mean two things.

Narrowing is the safe direction and widening is not. A bulk button handed a
scope it can't parse refuses rather than falling back to ``all`` — a typo there
would otherwise spend an agent run per report across the whole backlog. A scope
naming an agent nobody imported is fine: it selects nothing, which is the truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.db.models import Count, Q

from franktheunicorn.core.models import SecurityReport

SCOPE_ALL = "all"
SCOPE_HUMAN = "none"
SCOPE_ANY_AGENT = "any"

#: Scope keywords that are not agent names, and therefore can't be one.
RESERVED_SCOPES = (SCOPE_ALL, SCOPE_HUMAN, SCOPE_ANY_AGENT)

#: ``SecurityReport.source_agent``'s max_length, spelled once.
MAX_AGENT_NAME_LEN = 100

#: Any run outside ``[a-z0-9._]`` — hyphens included, so "claude--code" and
#: "claude - code" fold with "claude-code" — becomes one hyphen. Keeps names URL-
#: and filename-safe, so the list can echo a scope into its Export href and the
#: export can put it in a ``Content-Disposition`` without escaping either.
_NAME_JUNK = re.compile(r"[^a-z0-9._]+")


def normalize_agent_name(raw: str) -> str:
    """Fold *raw* to the stored spelling: lower case, ``[a-z0-9._-]`` only.

    "Claude Code" and "claude-code" have to be one filter. Two spellings of one
    agent would split its precision numbers in half and make a scoped button
    miss half its reports, and nothing would look wrong.
    """
    name = _NAME_JUNK.sub("-", raw.strip().lower()).strip("-.")
    return name[:MAX_AGENT_NAME_LEN].rstrip("-.")


def clean_agent_name(raw: str) -> str:
    """The name to store for *raw*, or "" for a human-sent report.

    Raises :class:`ValueError` with an operator-facing message for a reserved word
    or for input that folds to nothing. Silently storing "" there would file an
    agent's whole archive as human-sent, and that is the label that matters most.
    """
    if not raw.strip():
        return ""
    name = normalize_agent_name(raw)
    if not name:
        msg = (
            f"{raw.strip()!r} isn't usable as an agent name. Use letters, digits, dots "
            "or hyphens, like 'claude-code'."
        )
        raise ValueError(msg)
    if name in RESERVED_SCOPES:
        msg = (
            f"'{name}' is reserved: it's a filter, not an agent. Leave the field blank "
            "for reports a person sent."
        )
        raise ValueError(msg)
    return name


def parse_scope(raw: str | None) -> str | None:
    """The scope *raw* names, or None when it can't be one.

    Absent or blank is ``all``, the old behaviour. None means the caller has to
    decide, and a caller about to spend money should refuse.
    """
    if raw is None or not raw.strip():
        return SCOPE_ALL
    scope = normalize_agent_name(raw)
    return scope or None


def scope_q(scope: str) -> Q:
    """The filter behind *scope*. ``all`` is an empty ``Q``, so it adds no WHERE."""
    if scope == SCOPE_ALL:
        return Q()
    if scope == SCOPE_HUMAN:
        return Q(source_agent="")
    if scope == SCOPE_ANY_AGENT:
        # `__gt=""` rather than `~Q(source_agent="")`: the negated form can't use
        # the index. Same reasoning as SecurityReport.cve_without_branch_q.
        return Q(source_agent__gt="")
    return Q(source_agent=scope)


def scope_label(scope: str) -> str:
    """*scope* as words, for flash messages and confirm dialogs."""
    if scope == SCOPE_ALL:
        return "all reports"
    if scope == SCOPE_HUMAN:
        return "reports not from an agent"
    if scope == SCOPE_ANY_AGENT:
        return "reports from any agent"
    return f"reports from agent {scope}"


def scope_suffix(scope: str) -> str:
    """ " (reports from agent X)" for a flash message, or "" for ``all``.

    Empty for ``all`` on purpose. Every bulk button's flash predates scopes,
    and adding "(all reports)" to each one is noise when nothing was narrowed.
    """
    return "" if scope == SCOPE_ALL else f" ({scope_label(scope)})"


@dataclass(frozen=True)
class ScopeOption:
    """One entry in the list page's Source row."""

    key: str
    label: str
    count: int
    #: Shown on hover: how this source's reports were ruled. The point is the
    #: question an operator asks of another agent, "how often is it right?".
    breakdown: str = ""


def _breakdown(valid: int, rejected: int, total: int) -> str:
    open_count = total - valid - rejected
    return (
        f"{valid} ruled valid, {rejected} invalid / expected / duplicate, {open_count} still open"
    )


def scope_options() -> list[ScopeOption]:
    """The Source row: all, not-from-an-agent, any agent, then each agent by name.

    One grouped query for the per-agent rows. The three fixed rows are sums of
    it plus the human count, so the numbers in the row always add up.
    """
    rejected_q = Q(status__in=SecurityReport.NO_FIX_OWED_STATUSES)
    rows = list(
        SecurityReport.objects.values("source_agent")
        .annotate(
            total=Count("pk"),
            valid=Count("pk", filter=Q(status="valid")),
            rejected=Count("pk", filter=rejected_q),
        )
        .order_by("source_agent")
    )
    human = next((row for row in rows if row["source_agent"] == ""), None)
    agents = [row for row in rows if row["source_agent"] != ""]
    human_total = human["total"] if human else 0
    agent_total = sum(row["total"] for row in agents)

    options = [
        ScopeOption(SCOPE_ALL, "All", human_total + agent_total),
        ScopeOption(
            SCOPE_HUMAN,
            "Not from an agent",
            human_total,
            _breakdown(human["valid"], human["rejected"], human_total) if human else "",
        ),
    ]
    if agents:
        options.append(
            ScopeOption(
                SCOPE_ANY_AGENT,
                "Any agent",
                agent_total,
                _breakdown(
                    sum(row["valid"] for row in agents),
                    sum(row["rejected"] for row in agents),
                    agent_total,
                ),
            )
        )
    for row in agents:
        options.append(
            ScopeOption(
                row["source_agent"],
                row["source_agent"],
                row["total"],
                _breakdown(row["valid"], row["rejected"], row["total"]),
            )
        )
    return options


def known_agents() -> list[str]:
    """Every agent name in use, for the import forms' autocomplete."""
    return list(
        SecurityReport.objects.exclude(source_agent="")
        .values_list("source_agent", flat=True)
        .distinct()
        .order_by("source_agent")
    )
