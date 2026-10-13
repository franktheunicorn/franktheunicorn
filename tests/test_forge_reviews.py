"""Forge-client review listing and CI status for the intending-to-merge queue."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.backends.base import ForgeClient, ReviewBody
from franktheunicorn.backends.gitea import GiteaClient, _normalize_gitea_review
from franktheunicorn.backends.github import GitHubClient
from franktheunicorn.backends.gitlab import GitLabClient
from franktheunicorn.backends.mock import MockForgeClient


class _BareClient(ForgeClient):
    """The ABC with every abstract method stubbed, to test the defaults."""

    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        return []

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        return {}

    def get_pull_request_files(self, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return []

    def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
        return ""

    def create_review(
        self, owner: str, repo: str, pr_number: int, review: ReviewBody
    ) -> dict[str, Any]:
        return {}

    def get_review_comments(
        self, owner: str, repo: str, pr_number: int, review_id: int
    ) -> list[dict[str, Any]]:
        return []

    def get_issue_comments(
        self, owner: str, repo: str, issue_number: int, since: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    def delete_review_comment(self, owner: str, repo: str, pr_number: int, comment_id: int) -> None:
        pass

    def list_contributors(self, owner: str, repo: str) -> list[str]:
        return []

    def get_authenticated_user(self) -> dict[str, Any]:
        return {}

    def close(self) -> None:
        pass


class TestBaseDefaults:
    def test_reviews_default_empty(self) -> None:
        assert _BareClient().list_pull_request_reviews("o", "r", 1) == []

    def test_ci_default_unknown(self) -> None:
        assert _BareClient().get_commit_ci_status("o", "r", "abc") == ""


class TestGitHubReviews:
    @pytest.fixture
    def client(self) -> GitHubClient:
        c = GitHubClient(token="t", base_url="https://api.github.test")
        yield c
        c.close()

    def test_lists_reviews_single_page(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(json=[{"id": 1, "state": "APPROVED", "user": {"login": "h"}}])

        reviews = client.list_pull_request_reviews("apache", "spark", 7)

        assert [r["id"] for r in reviews] == [1]
        request = httpx_mock.get_requests()[0]
        assert request.url.path == "/repos/apache/spark/pulls/7/reviews"
        assert request.url.params["per_page"] == "100"

    def test_paginates_past_a_full_page(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        first = [{"id": i, "state": "COMMENTED"} for i in range(100)]
        httpx_mock.add_response(json=first)
        httpx_mock.add_response(json=[{"id": 100, "state": "APPROVED"}])

        reviews = client.list_pull_request_reviews("apache", "spark", 7)

        assert len(reviews) == 101
        assert reviews[-1]["state"] == "APPROVED"
        pages = [r.url.params["page"] for r in httpx_mock.get_requests()]
        assert pages == ["1", "2"]

    def test_raises_on_error(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        import httpx

        httpx_mock.add_response(status_code=404, json={"message": "Not Found"})
        with pytest.raises(httpx.HTTPStatusError):
            client.list_pull_request_reviews("apache", "spark", 7)


class TestGitHubCIStatus:
    @pytest.fixture
    def client(self) -> GitHubClient:
        c = GitHubClient(token="t", base_url="https://api.github.test")
        yield c
        c.close()

    def test_combines_check_runs_and_statuses(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        httpx_mock.add_response(
            url="https://api.github.test/repos/a/s/commits/abc/check-runs?per_page=100&page=1",
            json={"check_runs": [{"conclusion": "success"}]},
        )
        httpx_mock.add_response(
            url="https://api.github.test/repos/a/s/commits/abc/status?per_page=100",
            json={"statuses": [{"state": "pending", "context": "jenkins"}]},
        )

        assert client.get_commit_ci_status("a", "s", "abc") == "pending"

    def test_failure_in_statuses(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(
            url="https://api.github.test/repos/a/s/commits/abc/check-runs?per_page=100&page=1",
            json={"check_runs": []},
        )
        httpx_mock.add_response(
            url="https://api.github.test/repos/a/s/commits/abc/status?per_page=100",
            json={"statuses": [{"state": "failure"}]},
        )
        assert client.get_commit_ci_status("a", "s", "abc") == "fail"

    def test_paginates_check_runs(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        """A failure on the second page of check runs is still a failure."""
        base = "https://api.github.test/repos/a/s/commits/abc"
        httpx_mock.add_response(
            url=f"{base}/check-runs?per_page=100&page=1",
            json={"check_runs": [{"conclusion": "success"}] * 100},
        )
        httpx_mock.add_response(
            url=f"{base}/check-runs?per_page=100&page=2",
            json={"check_runs": [{"conclusion": "failure"}]},
        )
        httpx_mock.add_response(url=f"{base}/status?per_page=100", json={"statuses": []})

        assert client.get_commit_ci_status("a", "s", "abc") == "fail"

    def test_too_many_check_runs_is_unknown_not_green(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        base = "https://api.github.test/repos/a/s/commits/abc"
        for page in range(1, 11):
            httpx_mock.add_response(
                url=f"{base}/check-runs?per_page=100&page={page}",
                json={"check_runs": [{"conclusion": "success"}] * 100},
            )

        assert client.get_commit_ci_status("a", "s", "abc") == ""

    def test_empty_sha_makes_no_request(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        assert client.get_commit_ci_status("a", "s", "") == ""
        assert httpx_mock.get_requests() == []


class TestGitHubIssueComments:
    @pytest.fixture
    def client(self) -> GitHubClient:
        c = GitHubClient(token="t", base_url="https://api.github.test")
        yield c
        c.close()

    def test_paginates_past_a_full_page(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        """The approver's latest word on a 150-comment PR is on page two."""
        httpx_mock.add_response(json=[{"id": i, "body": "x"} for i in range(100)])
        httpx_mock.add_response(json=[{"id": 100, "body": "LGTM pending CI"}])

        comments = client.get_issue_comments("apache", "spark", 7)

        assert len(comments) == 101
        assert comments[-1]["body"] == "LGTM pending CI"
        pages = [r.url.params["page"] for r in httpx_mock.get_requests()]
        assert pages == ["1", "2"]

    def test_since_is_passed_on_every_page(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        httpx_mock.add_response(json=[])
        client.get_issue_comments("apache", "spark", 7, since="2026-10-01T00:00:00Z")
        request = httpx_mock.get_requests()[0]
        assert request.url.params["since"] == "2026-10-01T00:00:00Z"
        assert request.url.params["per_page"] == "100"


class TestGiteaReviews:
    @pytest.fixture
    def client(self) -> GiteaClient:
        c = GiteaClient(token="t", base_url="https://codeberg.test")
        yield c
        c.close()

    def test_lists_and_normalises(self, httpx_mock: HTTPXMock, client: GiteaClient) -> None:
        httpx_mock.add_response(
            json=[
                {
                    "id": 1,
                    "state": "REQUEST_CHANGES",
                    "user": {"login": "h"},
                    "body": "",
                    "submitted_at": "2026-10-01T10:00:00Z",
                    "commit_id": "abc",
                },
                {"id": 2, "state": "APPROVED", "user": {"login": "h"}, "dismissed": True},
                {"id": 3, "state": "COMMENT", "user": None},
            ]
        )

        reviews = client.list_pull_request_reviews("o", "r", 4)

        assert httpx_mock.get_requests()[0].url.path.endswith("/repos/o/r/pulls/4/reviews")
        assert [r["state"] for r in reviews] == ["CHANGES_REQUESTED", "DISMISSED", "COMMENTED"]
        assert reviews[1]["commit_id"] == ""
        assert reviews[2]["user"] == {"login": ""}

    def test_unknown_state_passes_through(self) -> None:
        assert _normalize_gitea_review({"state": "weird"})["state"] == "WEIRD"


class TestGitLabReviews:
    @pytest.fixture
    def client(self) -> GitLabClient:
        c = GitLabClient(token="t", base_url="https://gitlab.test")
        yield c
        c.close()

    def test_approvals_become_approved_reviews(
        self, httpx_mock: HTTPXMock, client: GitLabClient
    ) -> None:
        httpx_mock.add_response(
            json={
                "approved_by": [
                    {"user": {"username": "holdenk"}},
                    {"user": None},
                    "garbage",
                ]
            }
        )

        reviews = client.list_pull_request_reviews("grp", "proj", 9)

        # httpx decodes %2F in .path; the raw target keeps the encoded project id.
        raw = httpx_mock.get_requests()[0].url.raw_path.decode()
        assert raw.endswith("/projects/grp%2Fproj/merge_requests/9/approvals")
        assert reviews == [
            {
                "user": {"login": "holdenk"},
                "state": "APPROVED",
                "body": "",
                "submitted_at": "",
                "commit_id": "",
            }
        ]

    def test_no_approvals(self, httpx_mock: HTTPXMock, client: GitLabClient) -> None:
        httpx_mock.add_response(json={})
        assert client.list_pull_request_reviews("grp", "proj", 9) == []


class TestMockReviews:
    def test_fixture_backed(self, tmp_path: Path) -> None:
        (tmp_path / "o_r_pr5_reviews.json").write_text('[{"state": "APPROVED"}]')
        (tmp_path / "o_r_ci_abc.txt").write_text("pass\n")
        client = MockForgeClient(tmp_path)
        assert client.list_pull_request_reviews("o", "r", 5) == [{"state": "APPROVED"}]
        assert client.get_commit_ci_status("o", "r", "abc") == "pass"

    def test_defaults(self, tmp_path: Path) -> None:
        client = MockForgeClient(tmp_path)
        assert client.list_pull_request_reviews("o", "r", 5) == []
        assert client.get_commit_ci_status("o", "r", "abc") == ""
