"""OpenAI backend for review generation."""

from __future__ import annotations

from typing import Any

from franktheunicorn.review.backends.base import BaseLLMBackend


class OpenAIBackend(BaseLLMBackend):
    """Review backend using the OpenAI Python SDK."""

    _sdk_module = "openai"
    _default_key_env = "OPENAI_API_KEY"
    _default_model = "gpt-4o"

    # Modern OpenAI-compatible servers require `max_completion_tokens`; some
    # legacy vLLM builds only accept `max_tokens`. Start modern, fall back
    # on the first BadRequestError, then cache the survivor.
    _token_param: str = "max_completion_tokens"

    def _call_api(self, system_prompt: str, user_message: str, api_key: str) -> str:
        import openai

        kwargs: dict[str, object] = {"api_key": api_key}
        if self._config.base_url:
            kwargs["base_url"] = self._config.base_url

        client = openai.OpenAI(**kwargs)  # type: ignore[arg-type]

        def _create(token_param: str) -> Any:
            return client.chat.completions.create(  # type: ignore[call-overload]
                model=self._model,
                temperature=self._config.temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                **{token_param: self._config.max_tokens},
            )

        try:
            response = _create(self._token_param)
        except openai.BadRequestError as exc:
            alt = (
                "max_tokens"
                if self._token_param == "max_completion_tokens"
                else "max_completion_tokens"
            )
            if self._token_param in str(exc) or alt in str(exc):
                response = _create(alt)
                self._token_param = alt
            else:
                raise

        if hasattr(response, "usage") and response.usage:
            self._last_tokens_in = getattr(response.usage, "prompt_tokens", 0)
            self._last_tokens_out = getattr(response.usage, "completion_tokens", 0)
        return response.choices[0].message.content or "" if response.choices else ""
