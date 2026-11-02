"""Backports tab routing and the closed-PR banner on the detail page."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.test import Client

from franktheunicorn.backends.poller import _route_pr_to_queue
from franktheunicorn.review.checks.backport import declares_backport
from tests.factories import ProjectFactory, PullRequestFactory


def test_declares_backport() -> None:
    d = declares_backport
    assert d("[SPARK-1][3.5] Backport fix", "", [], "a", "b")
    assert d("Fix NPE [backport]", "", [], "a", "b")  # tag mid-title
    assert d("Fix", "Backport of #123", [], "a", "b")
    assert d("Fix", "This is a backport of the NPE fix.", [], "a", "b")  # cue in body, no #ref
    assert d("Fix", "Cherry-picked onto branch-3.5.", [], "a", "b")
    assert d("Fix", "", ["backport-3.5"], "a", "b")
    assert d("Fix", "", ["target/3.5-backport"], "a", "b")  # cue anywhere in label
    assert not d("Fix a bug", "plain body", ["bug"], "a", "b")
    assert not d("Fix", "should be backported to 3.5 later", [], "a", "b")
    assert not d("Fix", "please backport this to 3.5", [], "a", "b")
    assert not d("Fix", "should be cherry-picked to 3.5", [], "a", "b")
    assert not d("Fix", "We should backport #42 to 3.5", [], "a", "b")
    assert not d("Fix", "", ["needs-backport"], "a", "b")  # label on the original
    assert not d("Fix", "", ["backport-needed"], "a", "b")


@pytest.mark.django_db
def test_backport_routed_ahead_of_review_pile() -> None:
    pr = PullRequestFactory(title="Backport: fix npe", body="", labels=[])
    _route_pr_to_queue(pr, "someoneelse")
    assert "backport" in pr.queues


@pytest.mark.django_db
def test_backport_tab_lists_backports(client: Client) -> None:
    project = ProjectFactory()
    PullRequestFactory(project=project, number=1, queue="backport", queues=["backport"])
    PullRequestFactory(project=project, number=2, queue="review", queues=["review"])
    html = client.get("/?queue=backport").content.decode()
    assert "Backports" in html
    assert f"#{1}" in html


@pytest.mark.django_db
class TestClosedBanner:
    def test_stored_closed_shows_banner(self, client: Client) -> None:
        pr = PullRequestFactory(state="closed")
        html = client.get(f"/pr/{pr.pk}/").content.decode()
        assert "has been closed" in html

    def test_live_check_flips_open_row(self, client: Client) -> None:
        pr = PullRequestFactory(state="open")
        fake = MagicMock()
        fake.get_pull_request.return_value = {"state": "closed", "merged": True}
        with (
            patch("franktheunicorn.backends.make_client", return_value=fake),
            patch("franktheunicorn.config.resolver.get_forge_entry"),
        ):
            html = client.get(f"/pr/{pr.pk}/").content.decode()
        pr.refresh_from_db()
        assert pr.state == "merged"
        assert "has been merged" in html

    def test_open_has_no_banner(self, client: Client) -> None:
        pr = PullRequestFactory(state="open")
        fake = MagicMock()
        fake.get_pull_request.return_value = {"state": "open"}
        with (
            patch("franktheunicorn.backends.make_client", return_value=fake),
            patch("franktheunicorn.config.resolver.get_forge_entry"),
        ):
            html = client.get(f"/pr/{pr.pk}/").content.decode()
        assert "has been closed" not in html
