"""Which git tags are final releases.

``git tag --contains`` answers "which tags hold this commit", and a release
candidate is a tag too: ``v3.5.0-rc1`` holds a fix days or weeks before 3.5.0
ships, and a vote can sink it. ``fix_landed`` treats ``released`` as terminal,
so reading every tag as a release stamped a report "released" for good on
the strength of a candidate, and the sweep never looked again.

The rule is Apache Magpie's, from its release-management ``prepare`` skill
(``plugins/magpie-release-management/skills/prepare/scripts/prev_tag.py``,
``FINAL_RE`` and the ``--tag-prefix`` handling, at apache/magpie commit
dadb315caa8b881fc1d2195bef03223dac25915a, Apache-2.0; not at tag 0.1.0):

- a final release is ``[v]X.Y[.Z...]``, optionally with a ``.postN`` suffix;
- anything else that starts version-shaped (``3.5.0-rc1``, ``4.0.0-preview1``,
  ``3.0.0b1``) is a pre-release;
- a namespaced tag (``rel/1.2.3``, ``providers-amazon/8.0.0``) counts only
  under its configured prefix, and without one, tags containing ``/`` are
  not releases of this project. Flink's ``release-1.18.0`` needs a prefix
  too (``release-``): ``ProjectConfig.release_tag_prefix``.
"""

from __future__ import annotations

import re

FINAL = "final"
PRERELEASE = "prerelease"
OTHER = "other"

_FINAL_RE = re.compile(r"^v?(\d+(?:\.\d+)+)(?:[.-]?post(\d+))?$", re.IGNORECASE)
_VERSION_SHAPED_RE = re.compile(r"^v?\d+(?:\.\d+)+", re.IGNORECASE)


def classify_tag(name: str, prefix: str = "") -> str:
    """``final``, ``prerelease`` or ``other`` for one tag name."""
    tag = name.strip()
    if prefix:
        if not tag.startswith(prefix):
            return OTHER
        tag = tag[len(prefix) :]
    elif "/" in tag:
        return OTHER
    if _FINAL_RE.match(tag):
        return FINAL
    if _VERSION_SHAPED_RE.match(tag):
        return PRERELEASE
    return OTHER


def is_final_release_tag(name: str, prefix: str = "") -> bool:
    return classify_tag(name, prefix) == FINAL


def release_tag_prefix_for(full_name: str) -> str:
    """The configured ``release_tag_prefix`` for ``owner/repo``, or ``""``."""
    from franktheunicorn.config.loader import get_project_config

    config = get_project_config(full_name)
    return config.release_tag_prefix if config is not None else ""
