"""Base types for LLM review backends."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel
from pydantic import Field as PydanticField

logger = logging.getLogger(__name__)


@dataclass
class PRContext:
    """All the context an LLM needs to review a PR."""

    pr_title: str
    pr_body: str
    pr_author: str
    pr_number: int
    project_name: str
    review_context: str
    review_style: str
    tone: str
    test_expectations: str
    governance: str
    anti_patterns: list[str] = field(default_factory=list)


@dataclass
class ReviewFinding:
    """Unified finding format across all LLM backends."""

    file_path: str
    line_number: int | None
    title: str
    body: str
    suggestion: str = ""
    confidence: float = 0.5
    severity: str = "medium"


class FindingSchema(BaseModel):
    """Pydantic model for validating LLM JSON output."""

    file_path: str = ""
    line_number: int | None = None
    title: str = ""
    body: str = ""
    suggestion: str = ""
    confidence: float = PydanticField(default=0.5, ge=0.0, le=1.0)
    severity: str = "medium"


class LLMBackend(Protocol):
    """Protocol that all LLM backends must satisfy."""

    def generate_findings(
        self,
        diff: str,
        pr_context: PRContext,
    ) -> list[ReviewFinding]: ...


# Severity → confidence mapping (shared across backends).
SEVERITY_CONFIDENCE: dict[str, float] = {
    "critical": 0.9,
    "high": 0.8,
    "medium": 0.6,
    "low": 0.4,
    "nit": 0.3,
}


def parse_llm_response(raw_text: str) -> list[ReviewFinding]:
    """Parse JSON output from an LLM into validated ReviewFinding objects.

    Expects either a JSON array of finding objects or a JSON object with
    a ``findings`` key containing the array.
    """
    raw_text = raw_text.strip()
    if not raw_text:
        return []

    # Strip markdown code fences if present.
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        # Remove first and last fence lines.
        lines = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        raw_text = "\n".join(lines)

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.warning("LLM response is not valid JSON; returning empty findings.")
        return []

    # Accept {"findings": [...]} or [...]
    if isinstance(data, dict):
        data = data.get("findings", [])

    if not isinstance(data, list):
        logger.warning("LLM response JSON is not a list; returning empty findings.")
        return []

    findings: list[ReviewFinding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            validated = FindingSchema(**item)
            findings.append(
                ReviewFinding(
                    file_path=validated.file_path,
                    line_number=validated.line_number,
                    title=validated.title,
                    body=validated.body,
                    suggestion=validated.suggestion,
                    confidence=SEVERITY_CONFIDENCE.get(
                        validated.severity.lower(), validated.confidence
                    ),
                    severity=validated.severity.lower(),
                )
            )
        except Exception:
            logger.debug("Skipping invalid finding item: %s", item)
            continue

    return findings


__all__ = [
    "LLMBackend",
    "PRContext",
    "ReviewFinding",
    "parse_llm_response",
]
