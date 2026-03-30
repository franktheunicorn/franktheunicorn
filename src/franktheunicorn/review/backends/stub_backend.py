"""Stub backend — deterministic fake findings for testing without an LLM."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import PRContext, ReviewFinding

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig

_TEMPLATES = [
    "Consider adding a test for this change.",
    "This looks good overall. One minor suggestion: could the variable name be more descriptive?",
    "Nice improvement! Have you considered the edge case where the input is empty?",
    "The logic here could be simplified. Would you be open to a small refactor?",
    (
        "This change touches a critical path — might be worth adding a comment explaining"
        " the reasoning."
    ),
]


class StubBackend:
    """Deterministic stub backend for testing and demo mode."""

    def __init__(self, config: LLMBackendConfig) -> None:
        self._config = config

    def generate_findings(
        self,
        diff: str,
        pr_context: PRContext,
    ) -> list[ReviewFinding]:
        """Generate deterministic stub findings based on PR number."""
        findings: list[ReviewFinding] = []

        # Extract file paths from diff headers.
        file_paths: list[str] = []
        for line in diff.split("\n"):
            if line.startswith("+++ b/"):
                file_paths.append(line[6:])
        if not file_paths:
            file_paths = ["unknown_file.py"]

        for i, file_path in enumerate(file_paths[:2]):
            seed = f"{pr_context.pr_number}:{file_path}"
            bucket = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(_TEMPLATES)

            line_number = ((pr_context.pr_number * 7 + i * 13) % 50) + 1

            findings.append(
                ReviewFinding(
                    file_path=file_path,
                    line_number=line_number,
                    title=_TEMPLATES[bucket][:60],
                    body=_TEMPLATES[bucket],
                    confidence=min(0.5 + (bucket * 0.1), 1.0),
                    severity="medium",
                )
            )

        return findings
