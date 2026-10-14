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

- a final release is ``[v]X.Y[.Z...]``, optionally with a ``.postN`` suffix,
  and (frank's addition, for ASF podlings) an ``-incubating`` one;
- anything else that starts version-shaped (``3.5.0-rc1``, ``4.0.0-preview1``,
  ``3.0.0b1``, ``0.9.0-incubating-rc2``) is a pre-release;
- a namespaced or prefixed tag (``rel/1.2.3``, Flink's ``release-1.18.0``)
  counts only under its configured prefix, ``ProjectConfig.release_tag_prefix``,
  and without one a tag containing ``/`` is not a release of this project.

Some schemes no prefix expresses: Struts' ``STRUTS_7_4_0``, ``REL_5_2_5``, an
Airflow-style repo releasing several products. ``release_tag_pattern`` is a
regex a final tag must match in full, and replaces the version test when set.

Every tag still proves the commit is in published history, so ``fix_landed``
reads a pre-release or unrecognised tag as "merged"; only a final one is
"released".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FINAL = "final"
PRERELEASE = "prerelease"
OTHER = "other"

_FINAL_RE = re.compile(r"^v?(\d+(?:\.\d+)+)(?:[.-]?post(\d+))?(?:-incubating)?$", re.IGNORECASE)
_VERSION_SHAPED_RE = re.compile(r"^v?\d+(?:\.\d+)+", re.IGNORECASE)


@dataclass(frozen=True)
class ReleaseTagRule:
    """How one project names its final release tags."""

    prefix: str = ""
    pattern: str = ""

    def classify(self, name: str) -> str:
        """``final``, ``prerelease`` or ``other`` for one tag name."""
        tag = name.strip()
        if self.pattern and re.fullmatch(self.pattern, tag, re.IGNORECASE):
            return FINAL
        if self.prefix:
            if not tag.startswith(self.prefix):
                return OTHER
            tag = tag[len(self.prefix) :]
        elif "/" in tag:
            return OTHER
        if not self.pattern and _FINAL_RE.match(tag):
            return FINAL
        if _VERSION_SHAPED_RE.match(tag):
            return PRERELEASE
        return OTHER

    def describe(self) -> str:
        if self.pattern:
            return f"release_tag_pattern {self.pattern!r}"
        if self.prefix:
            return f"release_tag_prefix {self.prefix!r}"
        return "the default [v]X.Y.Z rule"


DEFAULT_RULE = ReleaseTagRule()


def classify_tag(name: str, prefix: str = "") -> str:
    """``final``, ``prerelease`` or ``other`` under a plain prefix rule."""
    return ReleaseTagRule(prefix=prefix).classify(name)


def is_final_release_tag(name: str, prefix: str = "") -> bool:
    return classify_tag(name, prefix) == FINAL


def release_tag_rule_for(full_name: str) -> ReleaseTagRule:
    """The configured rule for ``owner/repo``; the default when unconfigured."""
    from franktheunicorn.config.loader import get_project_config

    config = get_project_config(full_name)
    if config is None:
        return DEFAULT_RULE
    return ReleaseTagRule(prefix=config.release_tag_prefix, pattern=config.release_tag_pattern)
