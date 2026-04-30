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
    personality_identity: str = ""
    personality_internal_voice: str = ""
    personality_external_voice: str = ""
    personality_review_philosophy: str = ""
    linked_issues_context: str = ""
    # Repo health context (bootstrapped from git history analysis)
    repo_health_context: str = ""
    # v1.5 context injection fields
    community_context: str = ""
    jira_context: str = ""
    sentry_context: str = ""
    # v1: full-file + first-party-import context (built from local checkout)
    full_file_context: str = ""
    imported_modules_context: str = ""


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


@dataclass
class ReviewResult:
    """A single backend's review of a PR: an overall vibe + line findings."""

    overall_vibe: str = ""
    findings: list[ReviewFinding] = field(default_factory=list)


class LLMBackend(Protocol):
    """Protocol that all LLM backends must satisfy."""

    def generate_findings(
        self,
        diff: str,
        pr_context: PRContext,
    ) -> list[ReviewFinding]: ...

    def generate_review(
        self,
        diff: str,
        pr_context: PRContext,
    ) -> ReviewResult: ...


def _log_backend_error(backend_name: str, exc: BaseException) -> None:
    """Emit an appropriate log message for an LLM API error.

    HTTP 4xx errors get concise, actionable messages at the right level
    (WARNING for transient rate limits, ERROR for configuration problems).
    Unexpected errors get the full traceback via ``logger.exception``.
    """
    raw_code = getattr(exc, "status_code", None)
    # Some SDK exceptions (e.g. openai) expose status_code as a property that
    # reads from the underlying response object, which may be None or absent
    # when the error is constructed without a full response (e.g. during
    # connection errors before a response was received).
    status_code: int | None = raw_code if isinstance(raw_code, int) else None
    if status_code == 401:
        logger.error(
            "%s: authentication failed (HTTP 401) — check that your API key is "
            "correct and has not expired.",
            backend_name,
            exc_info=True,
        )
    elif status_code == 403:
        logger.error(
            "%s: permission denied (HTTP 403) — check that your API key has the "
            "required permissions and that your account is in good standing.",
            backend_name,
            exc_info=True,
        )
    elif status_code == 429:
        logger.warning(
            "%s: rate limited (HTTP 429) — the API quota was exceeded. "
            "Findings skipped for this cycle; will retry on the next poll.",
            backend_name,
            exc_info=True,
        )
    elif status_code is not None and 400 <= status_code < 500:
        logger.error(
            "%s: API call failed with HTTP %s — %s",
            backend_name,
            status_code,
            exc,
            exc_info=True,
        )
    else:
        logger.exception("%s API call failed.", backend_name)


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

    # Cost tracking: subclasses set these after _call_api to record usage.
    _last_tokens_in: int = 0
    _last_tokens_out: int = 0
    _last_duration: float | None = None

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
        if not self._sdk_available:
            return ReviewResult()

        api_key = self._resolve_api_key()
        if self._default_key_env and not api_key:
            key_env = self._config.api_key_env or self._default_key_env
            logger.error("API key not found in env var '%s'.", key_env)
            return ReviewResult()

        system_prompt = build_system_prompt(pr_context)
        user_message = build_user_message(diff, pr_context)

        import time

        start = time.monotonic()
        self._last_tokens_in = 0
        self._last_tokens_out = 0

        try:
            raw_text = self._call_api(system_prompt, user_message, api_key)
        except Exception as exc:
            _log_backend_error(type(self).__name__, exc)
            return ReviewResult()
        finally:
            self._last_duration = time.monotonic() - start

        return parse_llm_review(raw_text)

    def record_cost(
        self,
        project_id: int | None,
        pr_id: int | None,
        action_type: str = "review",
    ) -> None:
        """Record a CostRecord for the last API call. Safe to call unconditionally."""
        if not self._last_tokens_in and not self._last_tokens_out:
            return
        if project_id is None:
            return
        try:
            from franktheunicorn.core.models import CostRecord

            cost = _estimate_cost(
                self._config.provider,
                self._model,
                self._last_tokens_in,
                self._last_tokens_out,
            )
            CostRecord.objects.create(
                project_id=project_id,
                pull_request_id=pr_id,
                action_type=action_type,
                backend=f"{self._config.provider}/{self._model}",
                tokens_in=self._last_tokens_in,
                tokens_out=self._last_tokens_out,
                estimated_cost_usd=cost,
                duration_seconds=self._last_duration,
            )
        except Exception:
            logger.debug("Failed to record cost", exc_info=True)

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

# Matches the next JSON-value-start ('{' or '[') in arbitrary text.
_JSON_START_RE = re.compile(r"[{\[]")


def _extract_json_blob(text: str) -> object | None:
    """Find and decode the first JSON value embedded in arbitrary text.

    Models without strict JSON enforcement sometimes wrap output in prose
    ('Sure, here is the review: [...]' or '[...]\\n\\nLet me know!').
    raw_decode handles trailing junk; a single compiled regex jumps to
    the next '{' or '[' candidate in C, so we decode in place without
    per-character scanning or slicing.
    """
    decoder = json.JSONDecoder()
    pos = 0
    while (m := _JSON_START_RE.search(text, pos)) is not None:
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            pos = m.start() + 1
            continue
        return obj  # type: ignore[no-any-return]
    return None


def parse_llm_review(raw_text: str) -> ReviewResult:
    """Parse JSON output from an LLM into a ReviewResult.

    Expects either a JSON array of finding objects, or a JSON object with
    a ``findings`` key (and optional ``overall_vibe`` text summary).
    """
    raw_text = raw_text.strip()
    if not raw_text:
        return ReviewResult()

    # Strip markdown code fences if present.
    fence_match = _CODE_FENCE_RE.search(raw_text)
    if fence_match:
        raw_text = fence_match.group(1)

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        # Models without response_format enforcement may add prose around
        # the JSON. Try to extract the first valid JSON value.
        data = _extract_json_blob(raw_text)
        if data is None:
            logger.warning("LLM response is not valid JSON; returning empty findings.")
            return ReviewResult()

    overall_vibe = ""
    if isinstance(data, dict):
        raw_vibe = data.get("overall_vibe", "")
        if isinstance(raw_vibe, str):
            overall_vibe = raw_vibe.strip()
        data = data.get("findings", [])

    if not isinstance(data, list):
        logger.warning("LLM response JSON is not a list; returning empty findings.")
        return ReviewResult(overall_vibe=overall_vibe)

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

    return ReviewResult(overall_vibe=overall_vibe, findings=findings)


def parse_llm_response(raw_text: str) -> list[ReviewFinding]:
    """Parse JSON LLM output into ReviewFindings (vibe-stripped wrapper)."""
    return parse_llm_review(raw_text).findings


_COST_PER_MTOK: dict[str, tuple[float, float]] = {
    # (input_per_mtok, output_per_mtok) in USD
    "claude": (3.0, 15.0),
    "openai": (2.5, 10.0),
    "gemini": (1.25, 5.0),
    "ollama": (0.0, 0.0),
    "stub": (0.0, 0.0),
}


def _estimate_cost(provider: str, model: str, tokens_in: int, tokens_out: int) -> float:
    """Rough cost estimate in USD based on provider pricing."""
    rates = _COST_PER_MTOK.get(provider, (3.0, 15.0))
    cost = (tokens_in * rates[0] + tokens_out * rates[1]) / 1_000_000
    return round(cost, 6)


__all__ = [
    "LLMBackend",
    "PRContext",
    "ReviewFinding",
    "ReviewResult",
    "parse_llm_response",
    "parse_llm_review",
]
