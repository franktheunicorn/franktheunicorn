"""Anthropic Claude backend for review generation."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import PRContext, ReviewFinding, parse_llm_response
from franktheunicorn.review.prompt import build_system_prompt, build_user_message

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-sonnet-4-20250514"


class ClaudeBackend:
    """Review backend using the Anthropic Python SDK."""

    def __init__(self, config: LLMBackendConfig) -> None:
        self._config = config
        self._model = config.model or _DEFAULT_MODEL

    def generate_findings(
        self,
        diff: str,
        pr_context: PRContext,
    ) -> list[ReviewFinding]:
        try:
            import anthropic
        except ImportError:
            logger.error("anthropic package not installed. Run: pip install 'franktheunicorn[llm]'")
            return []

        api_key = os.environ.get(self._config.api_key_env or "ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.error(
                "API key not found in env var '%s'.",
                self._config.api_key_env or "ANTHROPIC_API_KEY",
            )
            return []

        client = anthropic.Anthropic(api_key=api_key)
        system_prompt = build_system_prompt(pr_context)
        user_message = build_user_message(diff, pr_context)

        try:
            response = client.messages.create(
                model=self._model,
                max_tokens=self._config.max_tokens,
                temperature=self._config.temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception:
            logger.exception("Anthropic API call failed.")
            return []

        raw_text = ""
        if response.content:
            first_block = response.content[0]
            if hasattr(first_block, "text"):
                raw_text = first_block.text
        return parse_llm_response(raw_text)
