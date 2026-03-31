"""
Three-source test identification for differential test verification (§9.1).

Identifies test files from:
1. Diff analysis — new/modified test files from the PR diff
2. PR description — LLM extraction of test references (stub for now)
3. Explicit callouts — tests tagged in PR template
"""

from __future__ import annotations

import re

_TEST_FILE_PATTERNS = (
    r"test_",
    r"_test\.",
    r"tests/",
    r"test/",
    r"spec/",
    r"_spec\.",
)


def identify_tests_from_diff(changed_files: list[str]) -> list[str]:
    """Source 1: Identify test files from the PR's changed files."""
    return [
        f
        for f in changed_files
        if any(re.search(pat, f, re.IGNORECASE) for pat in _TEST_FILE_PATTERNS)
    ]


def identify_tests_from_description(body: str) -> list[str]:
    """Source 2: Extract test references from PR description.

    Looks for common patterns like:
    - "Tests: test_foo.py"
    - "Test plan: run tests/test_bar.py"
    - Markdown checkboxes with test file references
    """
    tests: list[str] = []
    # Match file paths that look like test files.
    for match in re.finditer(r"[\w/]+(?:test_\w+|_test)\.\w+", body, re.IGNORECASE):
        tests.append(match.group(0))
    return tests


def identify_tests_from_template(body: str) -> list[str]:
    """Source 3: Extract from PR template callouts.

    Looks for sections like:
    ## Test Plan
    - [x] tests/test_foo.py
    """
    tests: list[str] = []
    in_test_section = False
    for line in body.split("\n"):
        if re.match(r"#{1,3}\s*test", line, re.IGNORECASE):
            in_test_section = True
            continue
        if in_test_section:
            if re.match(r"#{1,3}\s", line):
                in_test_section = False
                continue
            for match in re.finditer(r"[\w/]+\.\w+", line):
                path = match.group(0)
                if any(re.search(pat, path, re.IGNORECASE) for pat in _TEST_FILE_PATTERNS):
                    tests.append(path)
    return tests


def identify_test_scope(
    changed_files: list[str],
    pr_body: str,
) -> list[str]:
    """Union of all three sources, deduplicated."""
    tests = set()
    tests.update(identify_tests_from_diff(changed_files))
    tests.update(identify_tests_from_description(pr_body))
    tests.update(identify_tests_from_template(pr_body))
    return sorted(tests)
