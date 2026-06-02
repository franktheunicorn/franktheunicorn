"""Anthropic Claude backend for review generation."""

from __future__ import annotations

import time
from typing import Any, cast

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

    def _call_api_agentic(self, system_prompt: str, user_message: str, api_key: str) -> str:
        """Run a tool-use loop: let the model investigate, then emit findings.

        Bounded by ``max_iterations`` and ``time_budget_seconds`` (the sandbox
        enforces a third, independent budget). Token usage accumulates across
        turns so cost tracking captures the whole conversation.
        """
        import anthropic

        from franktheunicorn.review.agent_tools import dispatch_tool_use
        from franktheunicorn.review.prompt import tools_system_addendum

        cfg = self._tool_config
        runner = self._tool_runner
        assert cfg is not None  # only reached when tools are attached
        assert runner is not None

        client = anthropic.Anthropic(api_key=api_key)
        system = f"{system_prompt}\n\n{tools_system_addendum()}"
        messages: list[dict[str, object]] = [{"role": "user", "content": user_message}]
        final_text = ""
        deadline = time.monotonic() + cfg.time_budget_seconds

        for _ in range(cfg.max_iterations):
            if time.monotonic() > deadline:
                break
            response = client.messages.create(
                model=self._model,
                max_tokens=self._config.max_tokens,
                temperature=self._config.temperature,
                system=system,
                tools=cast("Any", self._tool_specs),
                messages=cast("Any", messages),
            )
            if hasattr(response, "usage") and response.usage:
                self._last_tokens_in += getattr(response.usage, "input_tokens", 0)
                self._last_tokens_out += getattr(response.usage, "output_tokens", 0)

            text_blocks = [
                getattr(b, "text", "")
                for b in response.content
                if getattr(b, "type", None) == "text"
            ]
            if text_blocks:
                final_text = "\n".join(text_blocks)

            if response.stop_reason != "tool_use":
                break

            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[dict[str, object]] = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                output = dispatch_tool_use(
                    self._tool_registry,
                    runner,
                    getattr(block, "name", ""),
                    dict(getattr(block, "input", None) or {}),
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": getattr(block, "id", ""),
                        "content": output,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        return final_text
