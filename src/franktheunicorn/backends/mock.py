"""
Mock forge client that returns fixture data from local JSON files.

Allows the entire system to be tested and demoed without a forge token
or network access. Fixture files live in ``configs/fixtures/`` and use
GitHub-shaped JSON; Forgejo-specific shape differences are not modeled
in mock mode (documented v1 limitation).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from franktheunicorn.backends.base import ForgeClient, ReviewBody

logger = logging.getLogger(__name__)


def _load_json_fixture(path: Path) -> list[dict[str, Any]]:
    """Load and parse a JSON fixture file."""
    with path.open() as f:
        result: list[dict[str, Any]] = json.load(f)
        return result


class MockForgeClient(ForgeClient):
    """Returns fixture data from local JSON files instead of calling a forge."""

    def __init__(self, fixtures_dir: str | Path) -> None:
        self._fixtures_dir = Path(fixtures_dir)

    def list_pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        """Load PRs from fixture file, falling back to built-in demo data."""
        fixture_path = self._fixtures_dir / f"{owner}_{repo}_pulls.json"
        if fixture_path.exists():
            return _load_json_fixture(fixture_path)

        logger.info("No fixture found at %s, using built-in demo data", fixture_path)
        return _builtin_demo_pulls(owner, repo)

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        """Load single PR detail from fixture or return demo data with mergeable."""
        fixture_path = self._fixtures_dir / f"{owner}_{repo}_pr{pr_number}.json"
        if fixture_path.exists():
            with fixture_path.open() as f:
                result: dict[str, Any] = json.load(f)
                return result
        return {
            "number": pr_number,
            "mergeable": True,
            "mergeable_state": "clean",
            "base": {
                "ref": "main",
                "sha": "0" * 40,
                "repo": {"full_name": f"{owner}/{repo}"},
            },
            "head": {
                "ref": f"pr-{pr_number}",
                "sha": "1" * 40,
                "repo": {
                    "clone_url": "",
                    "full_name": f"{owner}/{repo}",
                    "fork": False,
                },
            },
        }

    def get_pull_request_files(self, owner: str, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """Load PR files from fixture or return demo files."""
        fixture_path = self._fixtures_dir / f"{owner}_{repo}_pr{pr_number}_files.json"
        if fixture_path.exists():
            return _load_json_fixture(fixture_path)
        return [
            {"filename": "README.md", "additions": 5, "deletions": 2, "status": "modified"},
            {"filename": "src/main.py", "additions": 20, "deletions": 3, "status": "modified"},
        ]

    def get_pull_request_diff(self, owner: str, repo: str, pr_number: int) -> str:
        """Load diff from fixture or return stub."""
        fixture_path = self._fixtures_dir / f"{owner}_{repo}_pr{pr_number}.diff"
        if fixture_path.exists():
            return fixture_path.read_text()
        return "--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n+# Updated\n"

    def create_review(
        self, owner: str, repo: str, pr_number: int, review: ReviewBody
    ) -> dict[str, Any]:
        """Return a canned review-create response.

        ``comment_ids_by_key`` is empty in mock mode because no real
        forge-side IDs are generated.
        """
        return {
            "id": 1,
            "state": "COMMENTED",
            "body": review.body,
            "comment_ids_by_key": {},
        }

    def get_review_comments(
        self, owner: str, repo: str, pr_number: int, review_id: int
    ) -> list[dict[str, Any]]:
        """Return an empty list — mock mode doesn't track posted comments."""
        return []

    def get_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Load issue comments from fixture or return empty list."""
        fixture_path = self._fixtures_dir / f"{owner}_{repo}_issue_{issue_number}_comments.json"
        if fixture_path.exists():
            return _load_json_fixture(fixture_path)
        return []

    def delete_review_comment(self, owner: str, repo: str, pr_number: int, comment_id: int) -> None:
        """No-op in mock mode."""

    def list_contributors(self, owner: str, repo: str) -> list[str]:
        """Load contributors from fixture or return empty list."""
        fixture_path = self._fixtures_dir / f"{owner}_{repo}_contributors.json"
        if fixture_path.exists():
            data = _load_json_fixture(fixture_path)
            return [entry["login"] for entry in data if entry.get("login")]
        return []

    def get_authenticated_user(self) -> dict[str, Any]:
        """Return a mock authenticated user."""
        return {"login": "mock-user", "id": 0, "type": "User"}

    def close(self) -> None:
        pass


MockGitHubClient = MockForgeClient


def _builtin_demo_pulls(owner: str, repo: str) -> list[dict[str, Any]]:
    """Built-in demo PR data so the app works out of the box."""
    return [
        {
            "id": 1001,
            "number": 42,
            "title": "Fix flaky test in scheduler module",
            "user": {"login": "alice-dev"},
            "state": "open",
            "html_url": f"https://github.com/{owner}/{repo}/pull/42",
            "diff_url": f"https://github.com/{owner}/{repo}/pull/42.diff",
            "body": "This PR fixes a race condition in the scheduler tests.",
            "labels": [{"name": "bug"}, {"name": "tests"}],
            "requested_reviewers": [{"login": "holdenk"}],
            "draft": False,
            "created_at": "2026-03-20T10:00:00Z",
            "updated_at": "2026-03-27T14:30:00Z",
            "additions": 15,
            "deletions": 3,
        },
        {
            "id": 1002,
            "number": 43,
            "title": "Add support for new data source connector",
            "user": {"login": "bob-contributor"},
            "state": "open",
            "html_url": f"https://github.com/{owner}/{repo}/pull/43",
            "diff_url": f"https://github.com/{owner}/{repo}/pull/43.diff",
            "body": (
                "Adds a new connector for reading from Parquet files."
                "\n\nThis is my first contribution!"
            ),
            "labels": [{"name": "feature"}, {"name": "new-contributor"}],
            "requested_reviewers": [],
            "draft": False,
            "created_at": "2026-03-25T08:00:00Z",
            "updated_at": "2026-03-27T09:00:00Z",
            "additions": 250,
            "deletions": 10,
        },
        {
            "id": 1003,
            "number": 44,
            "title": "Update dependencies and CI config",
            "user": {"login": "dependabot[bot]"},
            "state": "open",
            "html_url": f"https://github.com/{owner}/{repo}/pull/44",
            "diff_url": f"https://github.com/{owner}/{repo}/pull/44.diff",
            "body": "Bumps `requests` from 2.31.0 to 2.32.0.",
            "labels": [{"name": "dependencies"}],
            "requested_reviewers": [],
            "draft": False,
            "created_at": "2026-03-26T06:00:00Z",
            "updated_at": "2026-03-26T06:00:00Z",
            "additions": 2,
            "deletions": 2,
        },
    ]
