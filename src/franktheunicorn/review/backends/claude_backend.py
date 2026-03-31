"""Anthropic Claude backend for review generation."""

from __future__ import annotations

from franktheunicorn.review.backends.base import BaseLLMBackend


class ClaudeBackend(BaseLLMBackend):
    """Review backend using the Anthropic Python SDK."""

    _sdk_module = "anthropic"
    _default_key_env = "ANTHROPIC_API_KEY"
    _default_model = "claude-sonnet-4-20250514"

    def _call_api(self, system_prompt: str, user_message: str, api_key: str) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=self._model,
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        if hasattr(response, "usage") and response.usage:
            self._last_tokens_in = getattr(response.usage, "input_tokens", 0)
            self._last_tokens_out = getattr(response.usage, "output_tokens", 0)
        if response.content:
            first_block = response.content[0]
            if hasattr(first_block, "text"):
                return first_block.text
        return ""
