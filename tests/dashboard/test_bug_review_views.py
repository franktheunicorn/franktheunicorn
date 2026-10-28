"""Tests for the Bug Review dashboard views."""

from __future__ import annotations

import pytest
from django.test import Client

from tests.factories import IntroducedBugFactory, ProjectFactory


@pytest.mark.django_db
class TestBugReviewList:
    def test_list_renders(self, client: Client) -> None:
        bug = IntroducedBugFactory()
        response = client.get("/bugs/")
        assert response.status_code == 200
        assert bug.title.encode() in response.content

    def test_empty_list_renders(self, client: Client) -> None:
        response = client.get("/bugs/")
        assert response.status_code == 200
        assert b"No bugs on this tab" in response.content

    def test_new_tab_shows_only_new(self, client: Client) -> None:
        new_bug = IntroducedBugFactory(title="New bug")
        fixed_bug = IntroducedBugFactory(title="Fixed bug", status="fixed")
        response = client.get("/bugs/?status=bug-review")
        assert response.status_code == 200
        assert new_bug.title.encode() in response.content
        assert fixed_bug.title.encode() not in response.content

    def test_fixed_tab_shows_only_fixed(self, client: Client) -> None:
        fixed_bug = IntroducedBugFactory(title="Fixed bug", status="fixed")
        new_bug = IntroducedBugFactory(title="New bug")
        response = client.get("/bugs/?status=fixed")
        assert response.status_code == 200
        assert fixed_bug.title.encode() in response.content
        assert new_bug.title.encode() not in response.content

    def test_project_filter(self, client: Client) -> None:
        project_a = ProjectFactory(owner="apache", repo="spark")
        project_b = ProjectFactory(owner="apache", repo="hadoop")
        bug_a = IntroducedBugFactory(project=project_a, title="Spark bug")
        bug_b = IntroducedBugFactory(project=project_b, title="Hadoop bug")
        response = client.get("/bugs/?project=apache/spark")
        assert response.status_code == 200
        assert bug_a.title.encode() in response.content
        assert bug_b.title.encode() not in response.content

    def test_all_tab_shows_everything(self, client: Client) -> None:
        new = IntroducedBugFactory(title="New")
        fixed = IntroducedBugFactory(title="Fixed", status="fixed")
        dismissed = IntroducedBugFactory(title="Dismissed", status="dismissed")
        response = client.get("/bugs/?status=all")
        assert response.status_code == 200
        assert new.title.encode() in response.content
        assert fixed.title.encode() in response.content
        assert dismissed.title.encode() in response.content


@pytest.mark.django_db
class TestBugReviewDetail:
    def test_detail_renders(self, client: Client) -> None:
        bug = IntroducedBugFactory(
            title="Off-by-one",
            description="Loop runs too few iterations.",
            proposed_patch="- for i in range(n):\n+ for i in range(n + 1):",
        )
        response = client.get(f"/bugs/{bug.id}/")
        assert response.status_code == 200
        assert b"Off-by-one" in response.content
        assert b"Loop runs too few" in response.content
        assert b"range(n + 1)" in response.content

    def test_detail_shows_file_and_lines(self, client: Client) -> None:
        bug = IntroducedBugFactory(file_path="src/main.py", line_start=10, line_end=20)
        response = client.get(f"/bugs/{bug.id}/")
        assert response.status_code == 200
        assert b"src/main.py" in response.content
        assert b"L10" in response.content


@pytest.mark.django_db
class TestBugReviewActions:
    def test_dismiss_marks_dismissed(self, client: Client) -> None:
        bug = IntroducedBugFactory(status="new")
        response = client.post(f"/bugs/{bug.id}/dismiss/")
        assert response.status_code == 302
        bug.refresh_from_db()
        assert bug.status == "dismissed"

    def test_fix_marks_fixed(self, client: Client) -> None:
        bug = IntroducedBugFactory(status="new")
        response = client.post(f"/bugs/{bug.id}/fix/")
        assert response.status_code == 302
        bug.refresh_from_db()
        assert bug.status == "fixed"

    def test_htmx_returns_status_text(self, client: Client) -> None:
        bug = IntroducedBugFactory(status="new")
        response = client.post(f"/bugs/{bug.id}/dismiss/", HTTP_HX_REQUEST="true")
        assert response.status_code == 200
        assert response.content == b"dismissed"
