"""Tests for _openai_chat_preflight, _seed_token_param_fallback, and _check_ssh_configs."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.worker.runner import _check_ssh_configs, _openai_chat_preflight


def _make_bad_request(message: str) -> object:
    import openai

    resp = MagicMock()
    resp.status_code = 400
    resp.headers = {}
    resp.text = message
    return openai.BadRequestError(message=message, response=resp, body={"error": message})


def _make_client_mock(side_effects: list[object]) -> MagicMock:
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = side_effects
    return mock_client


@pytest.mark.django_db
class TestOpenAIChatPreflight:
    _CLIENT_KW: dict[str, str] = {"api_key": "fake-key"}
    _MODEL = "cortex-model"
    _BASE_URL = "https://snowhouse.example.com/api/v2/cortex/v1"

    def _run(self, side_effects: list[object]) -> set[int]:
        disabled: set[int] = set()
        mock_client = _make_client_mock(side_effects)
        with patch("openai.OpenAI", return_value=mock_client):
            _openai_chat_preflight(
                None,
                self._CLIENT_KW,
                self._MODEL,
                self._BASE_URL,
                idx=1,
                masked="fa…ey",
                disabled=disabled,
            )
        return disabled

    def test_success_first_attempt_does_not_disable(self) -> None:
        disabled = self._run([None])
        assert disabled == set()

    def test_success_first_attempt_no_fallback_row(self) -> None:
        from franktheunicorn.core.models import LLMBackendFallback

        self._run([None])
        assert not LLMBackendFallback.objects.filter(
            provider="openai", model=self._MODEL, base_url=self._BASE_URL
        ).exists()

    def test_retries_on_max_tokens_deprecation_error(self) -> None:
        err = _make_bad_request("max_tokens is deprecated in favor of max_completion_tokens")
        disabled = self._run([err, None])
        assert disabled == set()

    def test_seeds_fallback_row_after_successful_retry(self) -> None:
        from franktheunicorn.core.models import LLMBackendFallback

        err = _make_bad_request("max_tokens is deprecated in favor of max_completion_tokens")
        self._run([err, None])
        row = LLMBackendFallback.objects.filter(
            provider="openai", model=self._MODEL, base_url=self._BASE_URL
        ).first()
        assert row is not None
        assert row.token_param == "max_completion_tokens"

    def test_disables_on_second_attempt_failure(self) -> None:
        err1 = _make_bad_request("max_tokens is deprecated in favor of max_completion_tokens")
        err2 = _make_bad_request("max_completion_tokens also not supported")
        disabled = self._run([err1, err2])
        assert 1 in disabled

    def test_disables_on_unrelated_400_without_retry(self) -> None:
        import openai

        mock_client = MagicMock()
        call_count = 0

        def _side_effect(**_kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 400
            resp.headers = {}
            resp.text = "model not found"
            raise openai.BadRequestError(
                message="model not found", response=resp, body={"error": "model not found"}
            )

        mock_client.chat.completions.create.side_effect = _side_effect
        disabled: set[int] = set()
        with patch("openai.OpenAI", return_value=mock_client):
            _openai_chat_preflight(
                None,
                self._CLIENT_KW,
                self._MODEL,
                self._BASE_URL,
                idx=1,
                masked="fa…ey",
                disabled=disabled,
            )
        assert 1 in disabled
        assert call_count == 1

    def test_disables_on_non_bad_request_exception(self) -> None:
        disabled = self._run([ConnectionError("timeout")])
        assert 1 in disabled


class TestCheckSshConfigs:
    """_check_ssh_configs probes SSH for each enabled SSH-mode tool at startup."""

    def _make_operator_config(
        self,
        *,
        coderabbit_ssh: bool = False,
        claude_cli_ssh: bool = False,
        snowflake_ssh: bool = False,
    ) -> object:
        from franktheunicorn.config.models import (
            ClaudeCLIConfig,
            CodeRabbitConfig,
            OperatorConfig,
            RemoteExecutionConfig,
            SnowflakeReviewConfig,
        )

        remote_ssh = RemoteExecutionConfig(mode="ssh", ssh_command=["sf", "workspace", "ssh"])
        return OperatorConfig(
            coderabbit=CodeRabbitConfig(enabled=coderabbit_ssh, remote=remote_ssh),
            claude_cli=ClaudeCLIConfig(enabled=claude_cli_ssh, remote=remote_ssh),
            snowflake_review=SnowflakeReviewConfig(enabled=snowflake_ssh, remote=remote_ssh),
        )

    @patch("franktheunicorn.review.tool_executor.RemoteSSHExecutor._probe_ssh", return_value=True)
    def test_ssh_ok_returns_empty_set(self, mock_probe: MagicMock) -> None:
        cfg = self._make_operator_config(claude_cli_ssh=True)
        failed = _check_ssh_configs(cfg)  # type: ignore[arg-type]
        assert failed == frozenset()
        assert mock_probe.call_count == 1

    @patch("franktheunicorn.review.tool_executor.RemoteSSHExecutor._probe_ssh", return_value=False)
    def test_ssh_fail_returns_tool_name(
        self, mock_probe: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = self._make_operator_config(coderabbit_ssh=True)
        with caplog.at_level("WARNING"):
            failed = _check_ssh_configs(cfg)  # type: ignore[arg-type]
        assert "coderabbit" in failed
        assert "preflight probe failed" in caplog.text

    @patch("franktheunicorn.review.tool_executor.RemoteSSHExecutor._probe_ssh", return_value=True)
    def test_disabled_tool_skipped(self, mock_probe: MagicMock) -> None:
        # All tools disabled — no probes should fire.
        cfg = self._make_operator_config()
        failed = _check_ssh_configs(cfg)  # type: ignore[arg-type]
        assert failed == frozenset()
        mock_probe.assert_not_called()

    @patch("franktheunicorn.review.tool_executor.RemoteSSHExecutor._probe_ssh", return_value=True)
    def test_local_mode_tool_skipped(self, mock_probe: MagicMock) -> None:
        from franktheunicorn.config.models import ClaudeCLIConfig, OperatorConfig

        # enabled=True but mode=local (default) — should not probe
        cfg = OperatorConfig(claude_cli=ClaudeCLIConfig(enabled=True))
        failed = _check_ssh_configs(cfg)  # type: ignore[arg-type]
        assert failed == frozenset()
        mock_probe.assert_not_called()
