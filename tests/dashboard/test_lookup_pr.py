"""Tests for the PR lookup view."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import Client

from franktheunicorn.core.models import PullRequest
from tests.factories import ProjectFactory


@pytest.mark.django_db
class TestLookupPR:
    def test_get_redirects_to_index(self, client: Client) -> None:
        response = client.get("/lookup/")
        assert response.status_code == 302
        assert response["Location"] == "/"

    def test_existing_pr_redirects_to_detail(self, client: Client, db_pr: PullRequest) -> None:
        response = client.post(
            "/lookup/",
            {"project": "apache/spark", "pr_number": "42"},
        )
        assert response.status_code == 302
        assert response["Location"] == f"/pr/{db_pr.pk}/"

    def test_missing_pr_triggers_ingest(self, client: Client, db_pr: PullRequest) -> None:
        # Build a stub return value without persisting to DB (pk from db_pr used as stand-in)
        stub_pr = db_pr
        with patch(
            "franktheunicorn.dashboard.views._ingest_single_pr", return_value=stub_pr
        ) as mock_ingest:
            response = client.post(
                "/lookup/",
                # Use a number that isn't in the DB
                {"project": "apache/spark", "pr_number": "9999"},
            )
        mock_ingest.assert_called_once_with("apache", "spark", 9999)
        assert response.status_code == 302
        assert response["Location"] == f"/pr/{stub_pr.pk}/"

    def test_bad_project_format_shows_error(self, client: Client) -> None:
        response = client.post("/lookup/", {"project": "noslash", "pr_number": "42"})
        assert response.status_code == 302
        assert response["Location"] == "/"
        messages = list(response.wsgi_request._messages)  # type: ignore[attr-defined]
        assert any("valid" in str(m).lower() for m in messages)

    def test_non_digit_pr_number_shows_error(self, client: Client) -> None:
        response = client.post("/lookup/", {"project": "apache/spark", "pr_number": "abc"})
        assert response.status_code == 302
        assert response["Location"] == "/"
        messages = list(response.wsgi_request._messages)  # type: ignore[attr-defined]
        assert any("valid" in str(m).lower() for m in messages)

    def test_ingest_failure_shows_error(self, client: Client) -> None:
        with patch(
            "franktheunicorn.dashboard.views._ingest_single_pr",
            side_effect=RuntimeError("forge down"),
        ):
            response = client.post(
                "/lookup/",
                {"project": "apache/spark", "pr_number": "555"},
            )
        assert response.status_code == 302
        assert response["Location"] == "/"
        messages = list(response.wsgi_request._messages)  # type: ignore[attr-defined]
        assert any("555" in str(m) for m in messages)

    def test_lookup_pr_not_in_db_for_wrong_project(
        self, client: Client, db_pr: PullRequest
    ) -> None:
        # db_pr is #42 under apache/spark; looking up #42 under other/proj should trigger ingest
        ProjectFactory(owner="other", repo="proj")
        stub_pr = db_pr
        with patch(
            "franktheunicorn.dashboard.views._ingest_single_pr", return_value=stub_pr
        ) as mock_ingest:
            response = client.post("/lookup/", {"project": "other/proj", "pr_number": "42"})
        mock_ingest.assert_called_once_with("other", "proj", 42)
        assert response.status_code == 302
