"""Shared fixtures for Sentry data access tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sentry_issues_response() -> list[dict[str, Any]]:
    return json.loads((FIXTURES_DIR / "sentry_issues_response.json").read_text())
