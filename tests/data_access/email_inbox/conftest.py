"""Fixtures for email inbox tests."""

from __future__ import annotations

import pytest

from franktheunicorn.config.models import SecurityEmailConfig


@pytest.fixture
def email_config() -> SecurityEmailConfig:
    return SecurityEmailConfig(
        enabled=True,
        imap_host="imap.example.com",
        imap_port=993,
        imap_user="security@example.com",
        imap_pass="testpass",
        use_ssl=True,
        folder="INBOX",
    )
