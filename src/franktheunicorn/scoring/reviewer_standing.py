"""Who is this reviewer to the project: committer, org member, or neither.

The dashboard marks an approval by its weight. An org member's name is bold;
a project committer's is bold with a trailing ``*``. Two sources, cheapest
first:

* ``ProjectConfig.committers`` — the operator's list of forge logins. It is
  authoritative for "committer" because the forge does not know: on GitHub
  every ASF committer is a MEMBER of ``apache``, whichever project they
  commit to.
* GitHub's ``author_association`` on the review or comment, stored on the row
  (``review_states[login]["author_association"]``,
  ``merge_intent_by_association``). ``MEMBER`` / ``OWNER`` means the
  reviewer is in the org that owns the repo. GitHub reports a private org
  membership as ``CONTRIBUTOR`` to tokens outside the org, so a member who
  hides their membership reads as plain; listing them as a committer still
  marks them.

No forge calls here: the reads already happened on the poll, and a template
render must not become N membership lookups.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: GitHub ``author_association`` values that mean "in the repo owner's org".
ORG_MEMBER_ASSOCIATIONS: frozenset[str] = frozenset({"MEMBER", "OWNER"})

STANDING_COMMITTER = "committer"
STANDING_MEMBER = "member"
STANDING_NONE = ""


@dataclass(frozen=True)
class Reviewer:
    """A login and its standing, for the ``reviewer`` template filter."""

    login: str
    standing: str = STANDING_NONE

    @property
    def is_member(self) -> bool:
        """Committers count as members: both render bold."""
        return self.standing in (STANDING_MEMBER, STANDING_COMMITTER)

    @property
    def is_committer(self) -> bool:
        return self.standing == STANDING_COMMITTER


def committer_set(committers: Iterable[str] | None) -> frozenset[str]:
    """Lower-cased logins, for case-insensitive matching (GitHub logins are)."""
    return frozenset(c.strip().lower() for c in (committers or ()) if c and c.strip())


def standing_for(login: str, association: str, committers: frozenset[str]) -> str:
    """``committer`` beats ``member`` beats nothing."""
    if login.lower() in committers:
        return STANDING_COMMITTER
    if (association or "").upper() in ORG_MEMBER_ASSOCIATIONS:
        return STANDING_MEMBER
    return STANDING_NONE


def _association_from(review_states: object, login: str) -> str:
    if not isinstance(review_states, Mapping):
        return ""
    entry = review_states.get(login)
    if not isinstance(entry, Mapping):
        # Logins are stored as the forge spelled them; tolerate a case drift.
        wanted = login.lower()
        entry = next(
            (v for k, v in review_states.items() if str(k).lower() == wanted),
            None,
        )
    if not isinstance(entry, Mapping):
        return ""
    return str(entry.get("author_association") or "")


def reviewers(
    logins: Iterable[str],
    review_states: object,
    committers: Iterable[str] | None,
) -> list[Reviewer]:
    """``logins`` in order, each with its standing read off ``review_states``."""
    known = committer_set(committers)
    return [
        Reviewer(login, standing_for(login, _association_from(review_states, login), known))
        for login in logins
        if login
    ]


def merge_intent_reviewer(
    login: str,
    association: str,
    review_states: object,
    committers: Iterable[str] | None,
) -> Reviewer | None:
    """The merge-intent approver's standing; ``None`` when there is no approver.

    The intent's own association wins; a row written before it was kept
    falls back to the reviewer's entry in ``review_states``.
    """
    if not login:
        return None
    known = committer_set(committers)
    assoc = association or _association_from(review_states, login)
    return Reviewer(login, standing_for(login, assoc, known))
