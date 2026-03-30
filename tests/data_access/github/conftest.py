"""Shared fixtures for GitHub data access tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def pr_api_json() -> dict[str, Any]:
    """PR metadata as returned by the GitHub REST API."""
    return json.loads((FIXTURES_DIR / "pr_42_api.json").read_text())


@pytest.fixture
def pr_files_api_json() -> list[dict[str, Any]]:
    """PR file changes as returned by the GitHub REST API."""
    return json.loads((FIXTURES_DIR / "pr_42_files_api.json").read_text())


@pytest.fixture
def pr_diff_text() -> str:
    """Raw unified diff text."""
    return (FIXTURES_DIR / "pr_42_diff.diff").read_text()


@pytest.fixture
def pr_reviews_api_json() -> list[dict[str, Any]]:
    """PR reviews as returned by the GitHub REST API."""
    return json.loads((FIXTURES_DIR / "pr_42_reviews_api.json").read_text())


@pytest.fixture
def pr_comments_api_json() -> list[dict[str, Any]]:
    """PR review comments as returned by the GitHub REST API."""
    return json.loads((FIXTURES_DIR / "pr_42_comments_api.json").read_text())


@pytest.fixture
def pr_scrape_html() -> str:
    """GitHub PR page HTML."""
    return (FIXTURES_DIR / "pr_42_scrape.html").read_text()
