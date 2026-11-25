"""A project's committers: the config's hand list plus what the forge said at boot.

Two sources, unioned and case-insensitive:

* ``ProjectConfig.committers`` — logins written by hand.
* ``Project.committers_cache`` — everyone with write access to the config's
  ``committer_repos``, read once at worker boot (worker/committers.py).

Pure: it reads the row the caller already holds and makes no query, so the
scorer can call it per PR and a test with no database can pass ``None``.
"""

from __future__ import annotations


def project_committers(project_config: object, project: object = None) -> list[str]:
    """Every committer login known for the project, config's spelling first."""
    found: list[str] = []
    seen: set[str] = set()
    for source in (
        getattr(project_config, "committers", None),
        getattr(project, "committers_cache", None),
    ):
        if not isinstance(source, list):
            continue
        for login in source:
            name = str(login).strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                found.append(name)
    return found


def committers_for_pr(project_config: object, pr: object) -> list[str]:
    """``project_committers`` for the project *pr* belongs to.

    The row comes off ``pr.project``; a PR whose project cannot be loaded (a
    stand-in object, a test without a database) gets the config's list alone.
    """
    try:
        project = getattr(pr, "project", None)
    except Exception:
        project = None
    return project_committers(project_config, project)
