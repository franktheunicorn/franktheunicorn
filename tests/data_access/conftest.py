"""Shared fixtures for data access tests."""

from __future__ import annotations

from collections.abc import Generator

import httpx
import pytest


@pytest.fixture
def http_client() -> Generator[httpx.Client, None, None]:
    """Shared HTTP client for data access tests."""
    client = httpx.Client()
    yield client
    client.close()
