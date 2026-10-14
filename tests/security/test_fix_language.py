"""The security-language scan over fix branch names.

What matters: a CVE id or a security phrase in a branch name is found whatever
the separators; the report's own identifiers are found when long enough to
mean something; an innocuous name is clean; and the per-branch form names the
branch so the operator knows which one leaks.
"""

from __future__ import annotations

from franktheunicorn.security.fix_language import (
    FRANK_SECURITY_KEYWORDS,
    MAGPIE_SECURITY_KEYWORDS,
    SECURITY_KEYWORDS,
    scan,
    scan_branches,
)


class TestScan:
    def test_innocuous_branch_is_clean(self) -> None:
        assert scan("bug_86-tidy-null-handling") == []

    def test_cve_id_is_found_and_upper_cased(self) -> None:
        assert scan("fix-cve-2026-12345-quick") == ["CVE-2026-12345"]

    def test_phrase_with_separators_matches(self) -> None:
        assert "sql injection" in scan("bug_12-sql-injection-fix")
        assert "path traversal" in scan("bug_12_path_traversal")

    def test_short_tokens_and_prose_words_match_as_substrings(self) -> None:
        assert "xss" in scan("bug_3-escape-xss-in-ui")
        assert "vuln" in scan("bug_3-vuln-fix")
        assert "advisory" in scan("bug_3-advisory-prep")

    def test_extra_terms_catch_the_reports_own_identifiers(self) -> None:
        hits = scan("bug_9-jane-doe-report", extra_terms=["Jane Doe", "CVE-2026-1", "ab"])
        assert "Jane Doe" in hits
        assert "CVE-2026-1" not in hits  # not in the text
        assert "ab" not in hits  # too short to mean anything

    def test_empty_text(self) -> None:
        assert scan("") == []

    def test_result_is_sorted_and_deduplicated(self) -> None:
        hits = scan("cve-2026-1111 CVE-2026-1111 security-fix security_fix")
        assert hits == ["CVE-2026-1111", "security fix"]

    def test_keyword_lists_are_distinct_and_union_is_the_scan_list(self) -> None:
        assert not set(MAGPIE_SECURITY_KEYWORDS) & set(FRANK_SECURITY_KEYWORDS)
        assert set(SECURITY_KEYWORDS) == set(MAGPIE_SECURITY_KEYWORDS) | set(
            FRANK_SECURITY_KEYWORDS
        )


class TestScanBranches:
    def test_each_hit_names_its_branch(self) -> None:
        # A real id has at least three digits after the year; "CVE-2026-7" is
        # not one and must not trip the scan.
        hits = scan_branches(
            ["bug_1-clean", "bug_1-xss-fix", "bug_1-cve-2026-7-x", "b-cve-2026-7777"]
        )
        assert hits == ["bug_1-xss-fix: xss", "b-cve-2026-7777: CVE-2026-7777"]

    def test_no_branches_no_hits(self) -> None:
        assert scan_branches([]) == []
