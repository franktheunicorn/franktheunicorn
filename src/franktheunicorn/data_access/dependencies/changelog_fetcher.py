"""Dual-path changelog fetchers for dependency version transitions.

Follows the existing DataFetcher pattern: API path first, scrape fallback.
Abstract ``ChangelogFetcher`` base enables per-ecosystem subclasses.
"""

from __future__ import annotations

import logging
from typing import Any

from bs4 import BeautifulSoup

from franktheunicorn.data_access.base import (
    DataFetcher,
    FetchMethod,
    NotFoundError,
)
from franktheunicorn.data_access.dependencies.types import (
    ChangelogEntry,
    Ecosystem,
    VersionTransition,
    detect_breaking_changes,
    detect_deprecations,
    extract_github_owner_repo,
    find_changelog_url,
    find_source_url,
    version_to_tag_candidates,
)

logger = logging.getLogger(__name__)

_PYPI_API_BASE = "https://pypi.org/pypi"
_PYPI_WEB_BASE = "https://pypi.org/project"
_GITHUB_API_BASE = "https://api.github.com"
_GITHUB_WEB_BASE = "https://github.com"


class ChangelogFetcher(DataFetcher[ChangelogEntry]):
    """Abstract base for ecosystem-specific changelog fetchers.

    Subclass and set ``ecosystem``, then implement ``fetch_via_api``
    and ``fetch_via_scrape`` for the ecosystem's package registry.
    """

    ecosystem: Ecosystem


class PythonChangelogFetcher(ChangelogFetcher):
    """Fetch changelog/release notes for Python packages.

    API path: PyPI JSON API → GitHub Releases API.
    Scrape path: PyPI project page → GitHub releases page.
    """

    ecosystem = Ecosystem.PYTHON

    def fetch_via_api(  # type: ignore[override]
        self,
        transition: VersionTransition,
    ) -> ChangelogEntry:
        """Fetch changelog via PyPI JSON API + GitHub Releases API."""
        package = transition.package_name
        version = transition.new_version or transition.old_version or ""

        # Step 1: Get package metadata from PyPI
        pypi_url = f"{_PYPI_API_BASE}/{package}/json"
        try:
            response = self._client.get(pypi_url)
            response.raise_for_status()
            pypi_data: dict[str, Any] = response.json()
        except Exception as exc:
            logger.info("PyPI API failed for %s: %s", package, exc)
            return self._entry_with_error(
                transition, FetchMethod.API, f"PyPI API error: {exc}"
            )

        info = pypi_data.get("info", {})
        project_urls = info.get("project_urls") or {}
        home_page = info.get("home_page") or ""

        source_url = find_source_url(project_urls, home_page)
        changelog_url = find_changelog_url(project_urls)
        gh_info = extract_github_owner_repo(source_url) if source_url else None

        # Step 2: Try to fetch GitHub release notes
        if gh_info and version:
            owner, repo = gh_info
            release_data = self._try_github_release_api(
                owner, repo, version, package
            )
            if release_data:
                body = release_data.get("body", "") or ""
                tag = release_data.get("tag_name", "")
                release_url = (
                    f"{_GITHUB_WEB_BASE}/{owner}/{repo}/releases/tag/{tag}"
                )
                return ChangelogEntry(
                    fetched_via=FetchMethod.API,
                    package_name=package,
                    old_version=transition.old_version or "",
                    new_version=transition.new_version or "",
                    release_notes=body,
                    changelog_url=release_url,
                    repository_url=source_url,
                    release_date=release_data.get("published_at", ""),
                    breaking_changes_detected=detect_breaking_changes(body),
                    deprecations_detected=detect_deprecations(body),
                )

        # Step 3: Fall back to changelog URL from PyPI metadata
        return ChangelogEntry(
            fetched_via=FetchMethod.API,
            package_name=package,
            old_version=transition.old_version or "",
            new_version=transition.new_version or "",
            changelog_url=changelog_url or source_url,
            repository_url=source_url,
            fetch_error="No GitHub release found" if gh_info else "No GitHub repo found",
        )

    def fetch_via_scrape(  # type: ignore[override]
        self,
        transition: VersionTransition,
    ) -> ChangelogEntry:
        """Fetch changelog by scraping PyPI + GitHub pages."""
        package = transition.package_name
        version = transition.new_version or transition.old_version or ""

        # Step 1: Scrape PyPI project page for repo URL
        pypi_url = f"{_PYPI_WEB_BASE}/{package}/"
        try:
            response = self._scrape_get(pypi_url)
            source_url = self._extract_github_url_from_pypi_html(response.text)
        except NotFoundError:
            return self._entry_with_error(
                transition, FetchMethod.SCRAPE, f"Package {package} not found on PyPI"
            )
        except Exception as exc:
            logger.info("PyPI scrape failed for %s: %s", package, exc)
            return self._entry_with_error(
                transition, FetchMethod.SCRAPE, f"PyPI scrape error: {exc}"
            )

        gh_info = extract_github_owner_repo(source_url) if source_url else None

        # Step 2: Scrape GitHub release page
        if gh_info and version:
            owner, repo = gh_info
            release_data = self._try_github_release_scrape(
                owner, repo, version, package
            )
            if release_data:
                body, tag, release_date = release_data
                release_url = (
                    f"{_GITHUB_WEB_BASE}/{owner}/{repo}/releases/tag/{tag}"
                )
                return ChangelogEntry(
                    fetched_via=FetchMethod.SCRAPE,
                    package_name=package,
                    old_version=transition.old_version or "",
                    new_version=transition.new_version or "",
                    release_notes=body,
                    changelog_url=release_url,
                    repository_url=source_url,
                    release_date=release_date,
                    breaking_changes_detected=detect_breaking_changes(body),
                    deprecations_detected=detect_deprecations(body),
                )

        return ChangelogEntry(
            fetched_via=FetchMethod.SCRAPE,
            package_name=package,
            old_version=transition.old_version or "",
            new_version=transition.new_version or "",
            changelog_url=source_url,
            repository_url=source_url,
            fetch_error="No GitHub release found" if gh_info else "No GitHub repo found",
        )

    # -- Internal helpers --

    def _try_github_release_api(
        self,
        owner: str,
        repo: str,
        version: str,
        package_name: str,
    ) -> dict[str, Any] | None:
        """Try tag candidates against GitHub Releases API until one matches."""
        candidates = version_to_tag_candidates(version, package_name, repo)
        for tag in candidates:
            url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/releases/tags/{tag}"
            try:
                response = self._api_get(url)
                return response.json()  # type: ignore[no-any-return]
            except (NotFoundError, Exception):
                continue
        return None

    def _try_github_release_scrape(
        self,
        owner: str,
        repo: str,
        version: str,
        package_name: str,
    ) -> tuple[str, str, str] | None:
        """Try tag candidates by scraping GitHub release pages.

        Returns (body_text, tag, release_date) or None.
        """
        candidates = version_to_tag_candidates(version, package_name, repo)
        for tag in candidates:
            url = f"{_GITHUB_WEB_BASE}/{owner}/{repo}/releases/tag/{tag}"
            try:
                response = self._client.get(url)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                body, release_date = self._parse_github_release_html(response.text)
                if body:
                    return (body, tag, release_date)
            except Exception:
                continue
        return None

    @staticmethod
    def _extract_github_url_from_pypi_html(html: str) -> str:
        """Extract GitHub repo URL from PyPI project page HTML.

        Looks for links under "Project links" headings.
        """
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if (
                isinstance(href, str)
                and "github.com" in href
                and extract_github_owner_repo(href) is not None
            ):
                return href
        return ""

    @staticmethod
    def _parse_github_release_html(html: str) -> tuple[str, str]:
        """Parse release notes and date from GitHub release page HTML.

        Returns (body_text, release_date).
        """
        soup = BeautifulSoup(html, "html.parser")

        # Release notes body
        body = ""
        markdown_body = soup.find(class_="markdown-body")
        if markdown_body:
            body = markdown_body.get_text(separator="\n", strip=True)

        # Release date
        release_date = ""
        time_elem = soup.find("relative-time")
        if time_elem and hasattr(time_elem, "get"):
            dt_val = time_elem.get("datetime", "")
            release_date = str(dt_val) if dt_val else ""

        return (body, release_date)

    @staticmethod
    def _entry_with_error(
        transition: VersionTransition,
        method: FetchMethod,
        error: str,
    ) -> ChangelogEntry:
        """Build a ChangelogEntry with an error message."""
        return ChangelogEntry(
            fetched_via=method,
            package_name=transition.package_name,
            old_version=transition.old_version or "",
            new_version=transition.new_version or "",
            fetch_error=error,
        )
