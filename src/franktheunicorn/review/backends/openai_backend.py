"""OpenAI backend for review generation."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import PRContext, ReviewFinding, parse_llm_response
from franktheunicorn.review.prompt import build_system_prompt, build_user_message

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-4o"


class OpenAIBackend:
    """Review backend using the OpenAI Python SDK."""

    def __init__(self, config: LLMBackendConfig) -> None:
        self._config = config
        self._model = config.model or _DEFAULT_MODEL

    def generate_findings(
        self,
        diff: str,
        pr_context: PRContext,
    ) -> list[ReviewFinding]:
        try:
            import openai
        except ImportError:
            logger.error("openai package not installed. Run: pip install 'franktheunicorn[llm]'")
            return []

        api_key = os.environ.get(self._config.api_key_env or "OPENAI_API_KEY", "")
        if not api_key:
            logger.error(
                "API key not found in env var '%s'.",
                self._config.api_key_env or "OPENAI_API_KEY",
            )
            return []

        kwargs: dict[str, object] = {"api_key": api_key}
        if self._config.base_url:
            kwargs["base_url"] = self._config.base_url

        client = openai.OpenAI(**kwargs)  # type: ignore[arg-type]
        system_prompt = build_system_prompt(pr_context)
        user_message = build_user_message(diff, pr_context)

        try:
            response = client.chat.completions.create(
                model=self._model,
                max_tokens=self._config.max_tokens,
                temperature=self._config.temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            )
        except Exception:
            logger.exception("OpenAI API call failed.")
            return []

        raw_text = response.choices[0].message.content or "" if response.choices else ""
        return parse_llm_response(raw_text)
