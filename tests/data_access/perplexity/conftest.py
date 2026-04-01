"""Shared fixtures for Perplexity data access tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def perplexity_general_response() -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "perplexity_general_response.json").read_text())


@pytest.fixture
def perplexity_technical_response() -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "perplexity_technical_response.json").read_text())
