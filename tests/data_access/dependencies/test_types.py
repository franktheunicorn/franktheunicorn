"""Tests for dependency types and helper functions."""

from __future__ import annotations

from franktheunicorn.data_access.dependencies.types import (
    detect_breaking_changes,
    detect_deprecations,
    extract_github_owner_repo,
    find_changelog_url,
    find_source_url,
    version_to_tag_candidates,
)


class TestExtractGithubOwnerRepo:
    def test_standard_url(self) -> None:
        assert extract_github_owner_repo("https://github.com/psf/requests") == ("psf", "requests")

    def test_url_with_trailing_slash(self) -> None:
        assert extract_github_owner_repo("https://github.com/numpy/numpy/") == ("numpy", "numpy")

    def test_url_with_tree_suffix(self) -> None:
        result = extract_github_owner_repo(
            "https://github.com/azure/azure-sdk-for-python/tree/main/sdk/storage"
        )
        assert result == ("azure", "azure-sdk-for-python")

    def test_url_with_git_suffix(self) -> None:
        assert extract_github_owner_repo("https://github.com/psf/requests.git") == (
            "psf",
            "requests",
        )

    def test_non_github_url(self) -> None:
        assert extract_github_owner_repo("https://gitlab.com/foo/bar") is None

    def test_incomplete_url(self) -> None:
        assert extract_github_owner_repo("https://github.com/psf") is None


class TestFindSourceUrl:
    def test_finds_source_key(self) -> None:
        urls = {"Source": "https://github.com/psf/requests", "Docs": "https://docs.example.com"}
        assert find_source_url(urls) == "https://github.com/psf/requests"

    def test_finds_repository_key_case_insensitive(self) -> None:
        urls = {"Repository": "https://github.com/apache/arrow"}
        assert find_source_url(urls) == "https://github.com/apache/arrow"

    def test_priority_order(self) -> None:
        urls = {
            "Homepage": "https://github.com/fallback/repo",
            "Source": "https://github.com/primary/repo",
        }
        assert find_source_url(urls) == "https://github.com/primary/repo"

    def test_falls_back_to_home_page(self) -> None:
        assert find_source_url({}, "https://github.com/foo/bar") == "https://github.com/foo/bar"

    def test_ignores_non_github_urls(self) -> None:
        urls = {"Source": "https://gitlab.com/foo/bar"}
        assert find_source_url(urls) == ""

    def test_none_project_urls(self) -> None:
        assert find_source_url(None) == ""


class TestFindChangelogUrl:
    def test_finds_changelog(self) -> None:
        urls = {"Changelog": "https://example.com/CHANGELOG.md"}
        assert find_changelog_url(urls) == "https://example.com/CHANGELOG.md"

    def test_case_insensitive(self) -> None:
        urls = {"release notes": "https://example.com/releases"}
        assert find_changelog_url(urls) == "https://example.com/releases"

    def test_returns_empty_when_not_found(self) -> None:
        urls = {"Homepage": "https://example.com"}
        assert find_changelog_url(urls) == ""


class TestVersionToTagCandidates:
    def test_basic_candidates(self) -> None:
        candidates = version_to_tag_candidates("2.31.0")
        assert candidates[0] == "v2.31.0"
        assert candidates[1] == "2.31.0"
        assert "release-2.31.0" in candidates
        assert "rel_2_31_0" in candidates

    def test_with_package_name(self) -> None:
        candidates = version_to_tag_candidates("1.0.0", package_name="requests")
        assert "requests-v1.0.0" in candidates
        assert "requests-1.0.0" in candidates

    def test_with_repo_name(self) -> None:
        candidates = version_to_tag_candidates("1.0.0", package_name="pyarrow", repo_name="arrow")
        assert "arrow-1.0.0" in candidates
        assert "arrow-v1.0.0" in candidates
        # Also has package name candidates
        assert "pyarrow-1.0.0" in candidates

    def test_no_duplicates(self) -> None:
        candidates = version_to_tag_candidates("1.0.0", package_name="arrow", repo_name="arrow")
        # "arrow-1.0.0" should appear only once
        assert candidates.count("arrow-1.0.0") == 1

    def test_deduplication_preserves_order(self) -> None:
        candidates = version_to_tag_candidates("1.0.0")
        assert candidates == list(dict.fromkeys(candidates))


class TestDetectBreakingChanges:
    def test_detects_breaking(self) -> None:
        assert detect_breaking_changes("This is a breaking change.")
        assert detect_breaking_changes("Removed old API endpoint.")
        assert detect_breaking_changes("No longer supported.")

    def test_no_false_positives(self) -> None:
        assert not detect_breaking_changes("Added new feature. Fixed bug.")


class TestDetectDeprecations:
    def test_detects_deprecations(self) -> None:
        assert detect_deprecations("Deprecated old function.")
        assert detect_deprecations("This is a deprecation warning.")

    def test_no_false_positives(self) -> None:
        assert not detect_deprecations("Added new feature. Fixed bug.")
