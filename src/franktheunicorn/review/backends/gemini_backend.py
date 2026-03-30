"""Google Gemini backend for review generation (using google-genai SDK)."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import PRContext, ReviewFinding, parse_llm_response
from franktheunicorn.review.prompt import build_system_prompt, build_user_message

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiBackend:
    """Review backend using the google-genai Python SDK."""

    def __init__(self, config: LLMBackendConfig) -> None:
        self._config = config
        self._model = config.model or _DEFAULT_MODEL

    def generate_findings(
        self,
        diff: str,
        pr_context: PRContext,
    ) -> list[ReviewFinding]:
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            logger.error(
                "google-genai package not installed. Run: pip install 'franktheunicorn[llm]'"
            )
            return []

        api_key = os.environ.get(self._config.api_key_env or "GOOGLE_API_KEY", "")
        if not api_key:
            logger.error(
                "API key not found in env var '%s'.",
                self._config.api_key_env or "GOOGLE_API_KEY",
            )
            return []

        client = genai.Client(api_key=api_key)
        system_prompt = build_system_prompt(pr_context)
        user_message = build_user_message(diff, pr_context)

        try:
            response = client.models.generate_content(
                model=self._model,
                contents=user_message,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=self._config.temperature,
                    max_output_tokens=self._config.max_tokens,
                    response_mime_type="application/json",
                ),
            )
        except Exception:
            logger.exception("Gemini API call failed.")
            return []

        raw_text = response.text or ""
        return parse_llm_response(raw_text)
