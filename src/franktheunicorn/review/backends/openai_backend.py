"""OpenAI backend for review generation."""

from __future__ import annotations

import logging
from typing import Any

from franktheunicorn.review.backends.base import BaseLLMBackend

logger = logging.getLogger(__name__)

# Reinforces the JSON-only contract when response_format enforcement isn't
# available — small/older models tend to add prose preamble otherwise.
_JSON_ONLY_REMINDER = (
    "\n\nIMPORTANT: Respond with ONLY the JSON object described above. "
    "No prose, no explanation, no markdown fences — just the raw JSON."
)


class OpenAIBackend(BaseLLMBackend):
    """Review backend using the OpenAI Python SDK."""

    _sdk_module = "openai"
    _default_key_env = "OPENAI_API_KEY"
    _default_model = "gpt-4o"

    # Modern OpenAI-compatible servers require `max_completion_tokens`; some
    # legacy vLLM builds only accept `max_tokens`. Start modern, fall back
    # on the first BadRequestError, then cache the survivor.
    _token_param: str = "max_completion_tokens"

    # Some OpenAI-compatible servers (older vLLM, llama.cpp, certain proxies)
    # don't accept `response_format={"type": "json_object"}`. Start optimistic,
    # fall back to a plain prompt-only JSON request and cache the result.
    _supports_json_object: bool = True

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._load_fallback_state()

    def _load_fallback_state(self) -> None:
        """Load persisted compatibility flags from DB into instance attributes."""
        try:
            from franktheunicorn.core.models import LLMBackendFallback

            row = LLMBackendFallback.objects.filter(
                provider="openai",
                model=self._model,
                base_url=self._config.base_url or "",
            ).first()
            if row is not None:
                self._token_param = row.token_param
                self._supports_json_object = row.supports_json_object
        except Exception:
            logger.debug("Could not load LLM fallback state from DB.", exc_info=True)

    def _persist_fallback_state(self) -> None:
        """Upsert current compatibility flags to DB."""
        try:
            from franktheunicorn.core.models import LLMBackendFallback

            LLMBackendFallback.objects.update_or_create(
                provider="openai",
                model=self._model,
                base_url=self._config.base_url or "",
                defaults={
                    "token_param": self._token_param,
                    "supports_json_object": self._supports_json_object,
                },
            )
        except Exception:
            logger.debug("Could not persist LLM fallback state to DB.", exc_info=True)

    def _call_api(self, system_prompt: str, user_message: str, api_key: str) -> str:
        import openai

        kwargs: dict[str, object] = {"api_key": api_key}
        if self._config.base_url:
            kwargs["base_url"] = self._config.base_url

        client = openai.OpenAI(**kwargs)  # type: ignore[arg-type]

        def _create() -> Any:
            effective_system = (
                system_prompt if self._supports_json_object else system_prompt + _JSON_ONLY_REMINDER
            )
            request_kwargs: dict[str, Any] = {
                "model": self._model,
                "temperature": self._config.temperature,
                "messages": [
                    {"role": "system", "content": effective_system},
                    {"role": "user", "content": user_message},
                ],
                self._token_param: self._config.max_tokens,
            }
            if self._supports_json_object:
                request_kwargs["response_format"] = {"type": "json_object"}
            return client.chat.completions.create(**request_kwargs)

        # Retry once per known compatibility quirk (token-param name and
        # response_format support). Cap attempts so an unrelated 400 can't loop.
        last_exc: openai.BadRequestError | None = None
        for _ in range(3):
            try:
                response = _create()
                break
            except openai.BadRequestError as exc:
                msg = str(exc).lower()
                if self._supports_json_object and (
                    "response_format" in msg
                    or "response format" in msg
                    or "json_object" in msg
                    or "json object" in msg
                ):
                    logger.debug(
                        "Server rejected response_format=json_object; "
                        "falling back to plain JSON prompting for %s.",
                        self._model,
                    )
                    self._supports_json_object = False
                    self._persist_fallback_state()
                    last_exc = exc
                    continue
                alt = (
                    "max_tokens"
                    if self._token_param == "max_completion_tokens"
                    else "max_completion_tokens"
                )
                if self._token_param in msg or alt in msg:
                    logger.debug(
                        "Server rejected %s; falling back to %s for %s.",
                        self._token_param,
                        alt,
                        self._model,
                    )
                    self._token_param = alt
                    self._persist_fallback_state()
                    last_exc = exc
                    continue
                raise
        else:
            assert last_exc is not None
            raise last_exc

        if hasattr(response, "usage") and response.usage:
            self._last_tokens_in = getattr(response.usage, "prompt_tokens", 0)
            self._last_tokens_out = getattr(response.usage, "completion_tokens", 0)
        return response.choices[0].message.content or "" if response.choices else ""
