"""OpenAI backend for review generation."""

from __future__ import annotations

from franktheunicorn.review.backends.base import BaseLLMBackend


class OpenAIBackend(BaseLLMBackend):
    """Review backend using the OpenAI Python SDK."""

    _sdk_module = "openai"
    _default_key_env = "OPENAI_API_KEY"
    _default_model = "gpt-4o"

    def _call_api(self, system_prompt: str, user_message: str, api_key: str) -> str:
        import openai

        kwargs: dict[str, object] = {"api_key": api_key}
        if self._config.base_url:
            kwargs["base_url"] = self._config.base_url

        client = openai.OpenAI(**kwargs)  # type: ignore[arg-type]
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
        if hasattr(response, "usage") and response.usage:
            self._last_tokens_in = getattr(response.usage, "prompt_tokens", 0)
            self._last_tokens_out = getattr(response.usage, "completion_tokens", 0)
        return response.choices[0].message.content or "" if response.choices else ""
