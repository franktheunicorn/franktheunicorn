"""Dual-path fetcher for a repository's PR description template."""

from __future__ import annotations

import base64
import logging
from typing import Any

from franktheunicorn.data_access.base import (
    GITHUB_API_BASE,
    DataFetcher,
    FetchMethod,
    NotFoundError,
)
from franktheunicorn.data_access.github.types import PRTemplateSummary

logger = logging.getLogger(__name__)

# Candidate paths checked in order. GitHub also supports a PULL_REQUEST_TEMPLATE/
# directory; we check the directory listing as a final fallback.
_TEMPLATE_PATHS = (
    ".github/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
    "pull_request_template.md",
)
_TEMPLATE_DIR = ".github/PULL_REQUEST_TEMPLATE"
_RAW_BASE = "https://raw.githubusercontent.com"


class TemplateFetcher(DataFetcher[PRTemplateSummary]):
    """Fetch a repo's PR description template via API or raw content URL."""

    def fetch_via_api(  # type: ignore[override]
        self, owner: str, repo: str
    ) -> PRTemplateSummary:
        contents_base = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents"

        for path in _TEMPLATE_PATHS:
            try:
                resp = self._api_get_json(f"{contents_base}/{path}")
                data: dict[str, Any] = resp.json()
                text = _decode_contents(data)
                if text:
                    logger.debug("Found PR template at %s/%s:%s (api)", owner, repo, path)
                    return PRTemplateSummary(fetched_via=FetchMethod.API, text=text)
            except NotFoundError:
                continue

        # Try directory listing
        try:
            resp = self._api_get_json(f"{contents_base}/{_TEMPLATE_DIR}")
            entries: list[dict[str, Any]] = resp.json()
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("name", "").endswith(".md"):
                        file_resp = self._api_get_json(entry["url"])
                        text = _decode_contents(file_resp.json())
                        if text:
                            logger.debug(
                                "Found PR template at %s/%s:%s/%s (api)",
                                owner,
                                repo,
                                _TEMPLATE_DIR,
                                entry["name"],
                            )
                            return PRTemplateSummary(fetched_via=FetchMethod.API, text=text)
        except NotFoundError:
            pass

        logger.debug("No PR template found for %s/%s (api)", owner, repo)
        return PRTemplateSummary(fetched_via=FetchMethod.API, text="")

    def fetch_via_scrape(  # type: ignore[override]
        self, owner: str, repo: str
    ) -> PRTemplateSummary:
        for path in _TEMPLATE_PATHS:
            url = f"{_RAW_BASE}/{owner}/{repo}/HEAD/{path}"
            try:
                resp = self._scrape_get(url)
                text = resp.text.strip()
                if text:
                    logger.debug("Found PR template at %s/%s:%s (scrape)", owner, repo, path)
                    return PRTemplateSummary(fetched_via=FetchMethod.SCRAPE, text=text)
            except NotFoundError:
                continue

        # Try the directory — GitHub doesn't serve directory listings via raw URL,
        # so check a few common filenames inside the PULL_REQUEST_TEMPLATE dir.
        for name in ("pull_request_template.md", "PULL_REQUEST_TEMPLATE.md", "default.md"):
            url = f"{_RAW_BASE}/{owner}/{repo}/HEAD/{_TEMPLATE_DIR}/{name}"
            try:
                resp = self._scrape_get(url)
                text = resp.text.strip()
                if text:
                    logger.debug(
                        "Found PR template at %s/%s:%s/%s (scrape)",
                        owner,
                        repo,
                        _TEMPLATE_DIR,
                        name,
                    )
                    return PRTemplateSummary(fetched_via=FetchMethod.SCRAPE, text=text)
            except NotFoundError:
                continue

        logger.debug("No PR template found for %s/%s (scrape)", owner, repo)
        return PRTemplateSummary(fetched_via=FetchMethod.SCRAPE, text="")


def _decode_contents(data: dict[str, Any]) -> str:
    """Decode a GitHub Contents API file response to plain text.

    Strips surrounding whitespace so the API path returns exactly what the
    scrape path (which also strips) returns for the same file — the dual-path
    contract requires observably identical output.
    """
    encoding = data.get("encoding", "")
    content = data.get("content", "")
    if not content:
        return ""
    if encoding == "base64":
        try:
            decoded = base64.b64decode(content.replace("\n", ""))
            return decoded.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""
    return str(content).strip()
