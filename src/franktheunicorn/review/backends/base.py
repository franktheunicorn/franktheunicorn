"""Base types for LLM review backends."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, Field, ValidationError

from franktheunicorn.review.prompt import build_system_prompt, build_user_message

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig

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


class ReviewFinding(BaseModel):
    """Unified finding format across all LLM backends.

    Also serves as the Pydantic validation schema for LLM JSON output.
    """

    file_path: str = ""
    line_number: int | None = None
    title: str = ""
    body: str = ""
    suggestion: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    severity: str = "medium"


class LLMBackend(Protocol):
    """Protocol that all LLM backends must satisfy."""

    def generate_findings(
        self,
        diff: str,
        pr_context: PRContext,
    ) -> list[ReviewFinding]: ...


class BaseLLMBackend:
    """Convenience base class for SDK-backed LLM backends.

    Subclasses only need to implement ``_call_api`` — the base handles
    prompt construction, response parsing, import checking, and API key
    resolution.
    """

    _sdk_module: str = ""  # e.g. "anthropic"
    _default_key_env: str = ""  # e.g. "ANTHROPIC_API_KEY"
    _default_model: str = ""

    def __init__(self, config: LLMBackendConfig) -> None:
        self._config = config
        self._model = config.model or self._default_model
        self._sdk_available = True
        if self._sdk_module:
            try:
                __import__(self._sdk_module)
            except ImportError:
                logger.error(
                    "%s package not installed. Run: pip install franktheunicorn",
                    self._sdk_module,
                )
                self._sdk_available = False

    def generate_findings(
        self,
        diff: str,
        pr_context: PRContext,
    ) -> list[ReviewFinding]:
        if not self._sdk_available:
            return []

        api_key = self._resolve_api_key()
        if self._default_key_env and not api_key:
            key_env = self._config.api_key_env or self._default_key_env
            logger.error("API key not found in env var '%s'.", key_env)
            return []

        system_prompt = build_system_prompt(pr_context)
        user_message = build_user_message(diff, pr_context)

        try:
            raw_text = self._call_api(system_prompt, user_message, api_key)
        except Exception:
            logger.exception("%s API call failed.", type(self).__name__)
            return []

        return parse_llm_response(raw_text)

    def _resolve_api_key(self) -> str:
        key_env = self._config.api_key_env or self._default_key_env
        return os.environ.get(key_env, "") if key_env else ""

    def _call_api(self, system_prompt: str, user_message: str, api_key: str) -> str:
        """Make the actual SDK call. Must be overridden by subclasses."""
        raise NotImplementedError


# Severity → default confidence mapping (used only when the LLM didn't
# provide an explicit confidence value).
SEVERITY_CONFIDENCE: dict[str, float] = {
    "critical": 0.9,
    "high": 0.8,
    "medium": 0.6,
    "low": 0.4,
    "nit": 0.3,
}

# Matches markdown code fences anywhere in the text (```json ... ```)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def parse_llm_response(raw_text: str) -> list[ReviewFinding]:
    """Parse JSON output from an LLM into validated ReviewFinding objects.

    Expects either a JSON array of finding objects or a JSON object with
    a ``findings`` key containing the array.
    """
    raw_text = raw_text.strip()
    if not raw_text:
        return []

    # Strip markdown code fences if present.
    fence_match = _CODE_FENCE_RE.search(raw_text)
    if fence_match:
        raw_text = fence_match.group(1)

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
            finding = ReviewFinding(**item)
            # Use severity-based confidence only when the LLM left it at
            # the default (0.5), meaning it didn't provide one explicitly.
            if finding.confidence == 0.5 and finding.severity.lower() in SEVERITY_CONFIDENCE:
                finding.confidence = SEVERITY_CONFIDENCE[finding.severity.lower()]
            finding.severity = finding.severity.lower()
            findings.append(finding)
        except ValidationError:
            logger.debug("Skipping invalid finding item: %s", item)
            continue

    return findings


__all__ = [
    "LLMBackend",
    "PRContext",
    "ReviewFinding",
    "parse_llm_response",
]
