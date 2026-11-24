"""The GitHub reads the watcher is built on: conditional requests, the
changed-since listing, and notifications."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from franktheunicorn.backends.base import NotificationsUnavailableError
from franktheunicorn.backends.conditional_cache import (
    MAX_ENTRY_BYTES,
    CachedReply,
    ConditionalCache,
)
from franktheunicorn.backends.github import GitHubClient, parse_github_timestamp

BASE = "https://api.github.test"


@pytest.fixture
def client() -> Iterator[GitHubClient]:
    c = GitHubClient(token="t", base_url=BASE)
    yield c
    c.close()


def _pr(number: int, updated: str, state: str = "open") -> dict[str, Any]:
    return {"number": number, "updated_at": updated, "state": state}


class TestConditionalRequests:
    def test_a_304_is_served_from_the_last_200(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        """The point of the cache: an unchanged resource costs no primary quota,
        and the caller can't tell it got a 304."""
        httpx_mock.add_response(json={"number": 7}, headers={"ETag": '"abc"'})
        httpx_mock.add_response(status_code=304, match_headers={"If-None-Match": '"abc"'})

        first = client.get_pull_request("apache", "spark", 7)
        second = client.get_pull_request("apache", "spark", 7)

        assert first == second == {"number": 7}
        assert "If-None-Match" not in httpx_mock.get_requests()[0].headers
        assert client._conditional.hits == 1

    def test_a_changed_resource_replaces_the_cached_body(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        httpx_mock.add_response(json={"v": 1}, headers={"ETag": '"one"'})
        httpx_mock.add_response(json={"v": 2}, headers={"ETag": '"two"'})
        httpx_mock.add_response(status_code=304, match_headers={"If-None-Match": '"two"'})

        assert [client.get_authenticated_user() for _ in range(3)] == [
            {"v": 1},
            {"v": 2},
            {"v": 2},
        ]

    def test_query_and_media_type_are_part_of_the_key(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        """A PR's JSON and its diff are one path; a 304 for one must not serve the other."""
        httpx_mock.add_response(json={"number": 7}, headers={"ETag": '"json"'})
        httpx_mock.add_response(text="diff --git", headers={"ETag": '"diff"'})

        client.get_pull_request("apache", "spark", 7)
        assert client.get_pull_request_diff("apache", "spark", 7) == "diff --git"
        assert "If-None-Match" not in httpx_mock.get_requests()[1].headers

    def test_an_error_forgets_the_entry(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(json={"n": 1}, headers={"ETag": '"e"'})
        httpx_mock.add_response(status_code=500)
        httpx_mock.add_response(json={"n": 2})

        client.get_authenticated_user()
        with pytest.raises(Exception):  # noqa: B017 - raise_for_status's HTTPStatusError
            client.get_authenticated_user()
        client.get_authenticated_user()
        assert "If-None-Match" not in httpx_mock.get_requests()[2].headers

    def test_the_cache_is_bounded_by_bytes_oldest_out(self) -> None:
        cache = ConditionalCache(max_bytes=10)
        cache.put("a", CachedReply("1", b"123456", ()))
        cache.put("b", CachedReply("2", b"123456", ()))
        assert cache.get("a") is None
        assert cache.get("b") is not None
        assert len(cache) == 1

    def test_a_huge_body_is_not_kept(self) -> None:
        cache = ConditionalCache()
        cache.put("big", CachedReply("1", b"x" * (MAX_ENTRY_BYTES + 1), ()))
        assert cache.get("big") is None


def test_a_client_knows_whether_it_has_a_token() -> None:
    """The watcher reads this: a 304 is free only for an authenticated request."""
    with_token = GitHubClient(token="t", base_url=BASE)
    without = GitHubClient(token="", base_url=BASE)
    try:
        assert with_token.authenticated is True
        assert without.authenticated is False
    finally:
        with_token.close()
        without.close()


class TestUpdatedSince:
    def test_stops_at_the_first_pr_older_than_the_watermark(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        httpx_mock.add_response(
            json=[
                _pr(3, "2026-11-24T10:05:00Z"),
                _pr(2, "2026-11-24T10:01:00Z", state="closed"),
                _pr(1, "2026-11-24T09:00:00Z"),
            ]
        )

        listing = client.list_pull_requests_updated_since(
            "apache", "spark", datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
        )

        assert [p["number"] for p in listing.items] == [3, 2]
        assert listing.complete
        params = httpx_mock.get_requests()[0].url.params
        assert params["state"] == "all"
        assert params["sort"] == "updated"
        assert params["direction"] == "desc"

    def test_pages_until_the_watermark_then_says_whether_it_got_there(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        page = [_pr(n, "2026-11-24T10:30:00Z") for n in range(client._UPDATED_PAGE)]
        httpx_mock.add_response(json=page)
        httpx_mock.add_response(json=page)

        listing = client.list_pull_requests_updated_since(
            "apache", "spark", datetime(2026, 11, 24, 10, 0, tzinfo=UTC), max_pages=2
        )

        assert len(listing.items) == 2 * client._UPDATED_PAGE
        assert not listing.complete, "the watermark was never reached"

    def test_no_watermark_reads_one_page(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(
            json=[_pr(n, "2026-11-24T10:30:00Z") for n in range(client._UPDATED_PAGE)]
        )

        listing = client.list_pull_requests_updated_since("apache", "spark", None)

        assert len(listing.items) == client._UPDATED_PAGE
        assert listing.complete
        assert len(httpx_mock.get_requests()) == 1

    def test_supported_only_where_implemented(self) -> None:
        from franktheunicorn.backends.gitea import GiteaClient

        gitea = GiteaClient(token="t", base_url="https://git.example.test")
        try:
            assert gitea.supports_updated_since is False
            with pytest.raises(NotImplementedError):
                gitea.list_pull_requests_updated_since("a", "b", None)
            with pytest.raises(NotImplementedError):
                gitea.list_notifications(None)
        finally:
            gitea.close()


def _notification(thread: str, kind: str = "PullRequest") -> dict[str, Any]:
    return {
        "id": thread,
        "reason": "mention",
        "updated_at": "2026-11-24T10:05:00Z",
        "subject": {"type": kind, "url": f"{BASE}/repos/apache/spark/pulls/{thread}"},
        "repository": {"full_name": "apache/spark"},
    }


class TestNotifications:
    def test_reads_participating_pull_requests_including_already_read(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        httpx_mock.add_response(
            json=[_notification("1"), _notification("2", kind="Issue")],
            headers={"X-Poll-Interval": "60"},
        )

        page = client.list_notifications(datetime(2026, 11, 24, 10, 0, tzinfo=UTC))

        assert [n["id"] for n in page.items] == ["1"]
        assert page.poll_interval == 60
        params = httpx_mock.get_requests()[0].url.params
        assert params["all"] == "true", "read on github.com is not seen by frank"
        assert params["participating"] == "true"
        assert params["since"] == "2026-11-24T10:00:00Z"
        assert httpx_mock.get_requests()[0].method == "GET", "never marks anything read"

    def test_a_burst_bigger_than_one_read_says_it_is_incomplete(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        full = [_notification(str(n)) for n in range(client._NOTIFICATION_PAGE)]
        for _ in range(client._NOTIFICATION_PAGES):
            httpx_mock.add_response(json=full)

        page = client.list_notifications(None)

        assert not page.complete
        assert len(httpx_mock.get_requests()) == client._NOTIFICATION_PAGES

    def test_a_short_page_is_complete(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(json=[_notification("1")])
        assert client.list_notifications(None).complete

    def test_a_fine_grained_token_is_unavailable_not_transient(
        self, httpx_mock: HTTPXMock, client: GitHubClient
    ) -> None:
        httpx_mock.add_response(
            status_code=403,
            json={"message": "Resource not accessible by personal access token"},
            headers={"X-RateLimit-Remaining": "4999"},
        )

        with pytest.raises(NotificationsUnavailableError) as exc:
            client.list_notifications(None)
        assert exc.value.status == 403
        assert "personal access token" in str(exc.value)

    def test_a_rate_limit_is_transient(self, httpx_mock: HTTPXMock, client: GitHubClient) -> None:
        httpx_mock.add_response(
            status_code=403,
            json={"message": "API rate limit exceeded"},
            headers={"X-RateLimit-Remaining": "0"},
        )

        with pytest.raises(Exception) as exc:
            client.list_notifications(None)
        assert not isinstance(exc.value, NotificationsUnavailableError)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-24T10:00:00Z", datetime(2026, 11, 24, 10, 0, tzinfo=UTC)),
        ("2026-11-24T10:00:00", datetime(2026, 11, 24, 10, 0, tzinfo=UTC)),
        ("not a date", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_github_timestamp(value: object, expected: datetime | None) -> None:
    assert parse_github_timestamp(value) == expected
