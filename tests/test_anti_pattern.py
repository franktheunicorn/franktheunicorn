"""Tests for the anti-pattern module."""

from __future__ import annotations

from franktheunicorn.anti_pattern import (
    list_anti_patterns,
    record_anti_pattern,
    record_shown,
    suppression_score,
)
from franktheunicorn.models import AntiPattern


def test_record_new_anti_pattern(db_session):
    ap = record_anti_pattern(db_session, label="nitpick", phrase="please fix this")
    db_session.flush()
    assert ap.label == "nitpick"
    assert ap.count == 1
    assert ap.probability == 1.0


def test_record_anti_pattern_normalises_phrase(db_session):
    ap = record_anti_pattern(db_session, label="test", phrase="  PLEASE  FIX  THIS  ")
    db_session.flush()
    assert ap.phrase == "please fix this"


def test_record_anti_pattern_increments_existing(db_session):
    record_anti_pattern(db_session, label="nitpick", phrase="please fix this")
    db_session.flush()

    record_shown(db_session, label="nitpick")
    db_session.flush()

    ap2 = record_anti_pattern(db_session, label="nitpick", phrase="please fix this")
    db_session.flush()

    # count=2, total_shown=3 → p ≈ 0.67
    assert ap2.count == 2
    assert ap2.total_shown == 3
    assert abs(ap2.probability - 2 / 3) < 0.01


def test_record_shown_decreases_probability(db_session):
    record_anti_pattern(db_session, label="pedantic", phrase="this could be cleaner")
    db_session.flush()
    # Show 4 more times without the operator flagging them.
    for _ in range(4):
        record_shown(db_session, label="pedantic")
    db_session.flush()
    ap = db_session.query(AntiPattern).filter_by(label="pedantic").first()
    assert ap is not None
    assert ap.probability < 0.5


def test_suppression_score_no_patterns(db_session):
    score = suppression_score(db_session, "This PR looks great!")
    assert score == 0.0


def test_suppression_score_match(db_session):
    record_anti_pattern(db_session, label="nitpick", phrase="please fix this")
    db_session.flush()
    score = suppression_score(db_session, "Please fix this before merging.")
    assert score > 0.0


def test_suppression_score_no_match(db_session):
    record_anti_pattern(db_session, label="nitpick", phrase="please fix this")
    db_session.flush()
    score = suppression_score(db_session, "Great work on the refactor!")
    assert score == 0.0


def test_suppression_score_project_scoped(db_session):
    record_anti_pattern(db_session, label="nitpick", phrase="style issue", project_slug="proj-a")
    db_session.flush()
    # Querying for proj-b should not match proj-a patterns.
    score = suppression_score(db_session, "style issue here", project_slug="proj-b")
    assert score == 0.0


def test_list_anti_patterns(db_session):
    record_anti_pattern(db_session, label="a", phrase="phrase a")
    record_anti_pattern(db_session, label="b", phrase="phrase b")
    db_session.flush()
    patterns = list_anti_patterns(db_session)
    assert len(patterns) == 2


def test_list_anti_patterns_project_filtered(db_session):
    record_anti_pattern(db_session, label="a", phrase="p", project_slug="proj-x")
    record_anti_pattern(db_session, label="b", phrase="q", project_slug="proj-y")
    db_session.flush()
    patterns = list_anti_patterns(db_session, project_slug="proj-x")
    assert len(patterns) == 1
    assert patterns[0].label == "a"
