"""Stub backend — deterministic fake findings for testing without an LLM."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import PRContext, ReviewFinding, ReviewResult

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig

_VIBES = [
    "Overall vibes: solid change, no major concerns. Worth a closer look at edge cases.",
    "Overall vibes: ambitious refactor — touches a lot, would benefit from extra tests.",
    "Overall vibes: small focused PR, easy to review, looks ready once nits are addressed.",
    "Overall vibes: useful feature but the design choices warrant a maintainer discussion.",
    "Overall vibes: cleanup PR with reasonable scope. Nothing alarming jumps out.",
]

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
        return self.generate_review(diff, pr_context).findings

    def generate_review(
        self,
        diff: str,
        pr_context: PRContext,
    ) -> ReviewResult:
        file_paths = [line[6:] for line in diff.split("\n") if line.startswith("+++ b/")]
        if not file_paths:
            file_paths = ["unknown_file.py"]

        findings: list[ReviewFinding] = []
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

        vibe_seed = f"vibe:{pr_context.pr_number}:{pr_context.project_name}"
        vibe_bucket = int(hashlib.sha256(vibe_seed.encode()).hexdigest(), 16) % len(_VIBES)
        return ReviewResult(overall_vibe=_VIBES[vibe_bucket], findings=findings)
