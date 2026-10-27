"""Magpie skill text injected into frank's agent prompts automatically.

The confinement guard (``tool_executor.guard_agent_argv``) moves the
checkout's ``.claude`` / ``.cursor`` / ``.codex`` / ``.pi`` aside for every
agent run, and the read-only review argv (``agent_cli``) carries no Skill
tool, so a Magpie skill installed as a *file* into a checkout is gone while
the agent runs and uninvokable even when it isn't. The documented route is
prompt text: :func:`vendored_text` returns the body of a vendored Magpie
markdown file, and the prompt builders drop it into the brief.

This module is the one door onto that for the three agent paths
(``review``, ``security``, ``verifier``, ``exploit``). A vendored file opts
into a path by listing it in its ``inject`` field; :func:`skills_block`
gathers every file whose ``inject`` contains the path and returns the
concatenated bodies under one header. Empty when nothing matches or the
project disabled it, so a checkout with no vendored skills — the default —
gets the same prompt it always did.
"""

from __future__ import annotations

import logging
from typing import Protocol

from franktheunicorn.magpie.pin import INJECT_TARGETS, load_pin, vendored_text

logger = logging.getLogger(__name__)


class _ProjectLike(Protocol):
    """Just enough of ``core.models.Project`` to resolve a config by name.

    ``full_name`` is a read-only property on the Django model, so the Protocol
    declares it the same way — a settable variable here would reject the real
    model under mypy (its field is a read-only descriptor).
    """

    @property
    def full_name(self) -> str: ...


def skills_block(*, inject_into: str, enabled: bool = True) -> str:
    """The concatenated Magpie skill text for one agent prompt path.

    ``inject_into`` is one of :data:`INJECT_TARGETS` (``"review"``,
    ``"security"``, ``"verifier"``, ``"exploit"``). Returns "" when the
    project disabled the feature (``enabled=False``), when no vendored file
    lists this path in its ``inject``, or when every matching file is
    missing from ``vendored/`` (each missing file is logged at WARNING by
    :func:`vendored_text`, since that means the lock and the tree disagree).

    The block is wrapped in a header that tells the agent what it is and,
    importantly, that it is borrowed rubric — authoritative where it speaks,
    but not a substitute for the project's own review guidance, which rides
    the same prompt separately. A file with no body after the provenance
    header is dropped, so an empty upstream stub does not become an empty
    section heading in the prompt.
    """
    # Validate before the enabled check: a caller bug (bad inject_into) must
    # surface even when the project disabled the feature, or it hides silently.
    if inject_into not in INJECT_TARGETS:
        msg = (
            f"inject_into {inject_into!r} is not one of {sorted(INJECT_TARGETS)}; "
            "this is a caller bug, not a config issue"
        )
        raise ValueError(msg)
    if not enabled:
        return ""

    pin = load_pin()
    matches = [vf for vf in pin.vendored if inject_into in vf.inject]
    if not matches:
        return ""

    bodies: list[str] = []
    for vf in matches:
        body = vendored_text(vf.dest).strip()
        if body:
            bodies.append(f"### {vf.purpose or vf.dest}\n\n{body}")
    if not bodies:
        return ""

    # INFO, not DEBUG: "injected N files" and "injected nothing" must not look
    # the same in the log (the same rule as the cycle summary's zero-findings
    # line). Per-PR detail stays at DEBUG elsewhere; this is one line per run.
    logger.info("Injecting %d Magpie skill file(s) into the %s prompt", len(bodies), inject_into)
    return _BLOCK_HEADER + "\n\n".join(bodies) + "\n"


_BLOCK_HEADER = """\
## Borrowed Magpie skill text (Apache Magpie, pinned)

The rubric below is borrowed from Apache Magpie at frank's pinned commit and
injected here automatically. It is authoritative where it speaks, but it does
not replace this project's own review guidance (which rides this prompt
separately); where the two disagree, the project's stance wins. Treat it as
background that sharpens the review, not as a checklist that gates it.

The text was written for Magpie's interactive sessions, which have tools and
workflow steps you do not have here. Disregard any reference to Magpie slash
commands (``<sm> ...``), vetted-ops, ``<project-config>/...`` file paths,
"Step N" workflow sequencing, or "draft a reporter reply" instructions — none
of that is in your environment, and none of it changes the reply format this
prompt asked for. Take the *reasoning rubric* (how to tell a real finding from
documented-as-trusted behavior, what makes a report VALID vs INVALID), and
ignore the workflow scaffolding around it.

"""


def skills_block_for_project(project: _ProjectLike | None, *, inject_into: str) -> str:
    """Resolve :func:`skills_block` from a ``Project`` model + its config.

    ``project`` is a ``core.models.Project`` (or None when the caller has no
    project — a report nobody attached). Loads the project's config and gates
    the block on its ``magpie_skills_enabled`` flag (default on, so a project
    with no config file gets the skills). The config lookup is lazy so this
    module stays importable from the security path without pulling Django's
    app registry at import time.
    """
    enabled = True
    if project is not None:
        from franktheunicorn.config.loader import get_project_config

        pc = get_project_config(project.full_name)
        if pc is not None:
            enabled = pc.magpie_skills_enabled
    return skills_block(inject_into=inject_into, enabled=enabled)
