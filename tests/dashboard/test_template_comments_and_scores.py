"""Tests for two display-only dashboard template fixes.

1. Multi-line ``{# ... #}`` comments leaked verbatim into rendered HTML because
   Django only strips *single-line* ``{# #}`` comments. They were converted to
   ``{% comment %} ... {% endcomment %}`` blocks, which never render.
2. Interest scores (stored as 0-1 floats) are displayed multiplied by 100 via
   the shared ``score100`` template filter — presentation only.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.test import Client

from franktheunicorn.core.models import PullRequest
from franktheunicorn.dashboard.templatetags.score_filters import score100


class TestMultiLineCommentLeak:
    """The multi-line comment blocks must not appear in rendered output."""

    @pytest.mark.django_db
    def test_index_does_not_leak_comment_text(self, client: Client, db_pr: PullRequest) -> None:
        response = client.get("/")
        assert response.status_code == 200
        content = response.content
        # No raw Django comment delimiters leak into the page.
        assert b"{#" not in content
        assert b"#}" not in content
        assert b"endcomment" not in content
        # The scary vendor-bump URL from the <head> comment must stay hidden.
        assert b"unpkg.com" not in content
        # The filter-bar note above the filter bar must not leak either.
        assert b"hx-select ensures" not in content
        # ...but the elements those comments document still render.
        assert b"dashboard/vendor/htmx.min.js" in content
        assert b"dashboard/styles.css" in content

    @pytest.mark.django_db
    def test_pr_detail_does_not_leak_comment_text(self, client: Client, db_pr: PullRequest) -> None:
        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        content = response.content
        assert b"{#" not in content
        assert b"unpkg.com" not in content
        assert b"endcomment" not in content


class TestScoreDisplayScaling:
    """Stored 0-1 scores render multiplied by 100, without being mutated."""

    @pytest.mark.django_db
    def test_index_shows_score_times_100(self, client: Client, db_pr: PullRequest) -> None:
        db_pr.interest_score = 0.15
        db_pr.save(update_fields=["interest_score"])

        response = client.get("/")
        assert response.status_code == 200
        text = response.content.decode()
        # Inspect the score badge itself so we don't collide with other "15"s
        # on the page (e.g. the "15+" additions count).
        badge_start = text.index("score-badge")
        badge_region = text[badge_start : badge_start + 120]
        assert "15" in badge_region
        # The raw 0-1 value is never shown.
        assert "0.15" not in text

    @pytest.mark.django_db
    def test_pr_detail_shows_score_times_100(self, client: Client, db_pr: PullRequest) -> None:
        db_pr.interest_score = 0.15
        db_pr.save(update_fields=["interest_score"])

        response = client.get(f"/pr/{db_pr.pk}/")
        assert response.status_code == 200
        content = response.content
        assert b'<span class="score-badge">15</span>' in content
        assert b"0.15" not in content

    @pytest.mark.django_db
    def test_render_does_not_mutate_stored_score(self, client: Client, db_pr: PullRequest) -> None:
        db_pr.interest_score = 0.15
        db_pr.save(update_fields=["interest_score"])

        client.get("/")
        client.get(f"/pr/{db_pr.pk}/")

        db_pr.refresh_from_db()
        # Presentation change only: the stored value is still 0.15.
        assert db_pr.interest_score == 0.15


class TestScore100Filter:
    """Unit coverage for the shared score-formatting filter."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.15, "15"),
            (0.0, "0"),
            (1.0, "100"),
            (0.157, "16"),  # rounds to nearest integer on the 0-100 scale
            (0.2, "20"),
        ],
    )
    def test_scales_and_rounds(self, value: float, expected: str) -> None:
        assert score100(value) == expected

    @pytest.mark.parametrize("value", [None, "not-a-number"])
    def test_non_numeric_renders_blank(self, value: Any) -> None:
        assert score100(value) == ""
