"""Detail-page views check the live PR state; never let a test reach a real forge."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _no_real_forge() -> Iterator[None]:
    # Default: still open. Tests that need a flip override this patch.
    fake = MagicMock()
    fake.get_pull_request.return_value = {"state": "open"}
    with patch("franktheunicorn.backends.make_client", return_value=fake):
        yield
