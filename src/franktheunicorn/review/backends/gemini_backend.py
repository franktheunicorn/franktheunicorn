"""Google Gemini backend for review generation (using google-genai SDK)."""

from __future__ import annotations

from franktheunicorn.review.backends.base import BaseLLMBackend


class GeminiBackend(BaseLLMBackend):
    """Review backend using the google-genai Python SDK."""

    _sdk_module = "google.genai"
    _default_key_env = "GOOGLE_API_KEY"
    _default_model = "gemini-2.5-flash"

    def _call_api(self, system_prompt: str, user_message: str, api_key: str) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
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
        return response.text or ""
