"""Tests for the shared backend preflight probe, token-param seeding, and SSH checks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.review.backends.preflight import probe_llm_backend
from franktheunicorn.worker.runner import _check_ssh_configs


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


def _openai_cfg(**overrides: object) -> object:
    from franktheunicorn.config.models import LLMBackendConfig

    base = dict(
        provider="openai",
        model="cortex-model",
        base_url="https://snowhouse.example.com/api/v2/cortex/v1",
        api_key_env="OPENAI_API_KEY",
    )
    base.update(overrides)
    return LLMBackendConfig(**base)  # type: ignore[arg-type]


@pytest.mark.django_db
class TestOpenAIChatPreflight:
    """The /models → chat-probe fallback lives in preflight now; the DB seeding
    of the discovered token-param name stays in runner's _check_backends."""

    _MODEL = "cortex-model"
    _BASE_URL = "https://snowhouse.example.com/api/v2/cortex/v1"

    def _run(self, side_effects: list[object]) -> object:
        import openai

        mock_client = _make_client_mock(side_effects)
        # /models is not implemented → NotFoundError → chat probe fallback.
        mock_client.models.list.side_effect = openai.NotFoundError(
            message="not found", response=MagicMock(), body=None
        )
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "fake-key"}),
            patch("openai.OpenAI", return_value=mock_client),
        ):
            return probe_llm_backend(_openai_cfg())

    def test_success_first_attempt_is_ok(self) -> None:
        result = self._run([None])
        assert result.ok is True
        # First-attempt success accepts the default max_tokens, so there is
        # nothing to seed — surfacing it would write a fallback row every boot.
        assert result.token_param is None

    def test_success_first_attempt_writes_no_fallback_row(self) -> None:
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
        from franktheunicorn.core.models import LLMBackendFallback
        from franktheunicorn.worker.runner import _check_backends

        mock_client = _make_client_mock([None])
        import openai

        mock_client.models.list.side_effect = openai.NotFoundError(
            message="not found", response=MagicMock(), body=None
        )
        cfg = OperatorConfig(
            github_username="testuser",
            llm_backends=[
                LLMBackendConfig(
                    provider="openai",
                    model="cortex-model",
                    base_url="https://snowhouse.example.com/api/v2/cortex/v1",
                    api_key_env="OPENAI_API_KEY",
                )
            ],
        )
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "fake-key"}),
            patch("openai.OpenAI", return_value=mock_client),
        ):
            _check_backends(cfg)
        assert not LLMBackendFallback.objects.filter(
            provider="openai", model="cortex-model"
        ).exists()

    def test_retries_on_max_tokens_deprecation_error(self) -> None:
        err = _make_bad_request("max_tokens is deprecated in favor of max_completion_tokens")
        result = self._run([err, None])
        assert result.ok is True
        assert result.token_param == "max_completion_tokens"

    def test_disables_on_second_attempt_failure(self) -> None:
        err1 = _make_bad_request("max_tokens is deprecated in favor of max_completion_tokens")
        err2 = _make_bad_request("max_completion_tokens also not supported")
        result = self._run([err1, err2])
        assert result.ok is False

    def test_disables_on_unrelated_400_without_retry(self) -> None:
        import openai

        mock_client = MagicMock()
        mock_client.models.list.side_effect = openai.NotFoundError(
            message="not found", response=MagicMock(), body=None
        )
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
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "fake-key"}),
            patch("openai.OpenAI", return_value=mock_client),
        ):
            result = probe_llm_backend(_openai_cfg())
        assert result.ok is False
        assert call_count == 1

    def test_disables_on_non_bad_request_exception(self) -> None:
        result = self._run([ConnectionError("timeout")])
        assert result.ok is False


@pytest.mark.django_db
class TestOllamaProbe:
    """Ollama used to be 'unchecked' — no probe — so a dead Ollama passed boot and
    failed on every report. It now gets a real reachability probe."""

    def test_a_reachable_ollama_is_ok(self) -> None:
        from franktheunicorn.config.models import LLMBackendConfig

        mock_client = MagicMock()
        mock_client.list.return_value = MagicMock(models=[])
        with patch("ollama.Client", return_value=mock_client):
            result = probe_llm_backend(
                LLMBackendConfig(provider="ollama", model="qwen2.5-coder:14b")
            )
        assert result.ok is True
        assert result.probed is True

    def test_a_dead_ollama_is_disabled(self) -> None:
        from franktheunicorn.config.models import LLMBackendConfig

        with patch("ollama.Client", side_effect=ConnectionError("connection refused")):
            result = probe_llm_backend(
                LLMBackendConfig(provider="ollama", model="qwen2.5-coder:14b")
            )
        assert result.ok is False
        assert "ConnectionError" in result.reason

    def test_stub_remains_unchecked(self) -> None:
        from franktheunicorn.config.models import LLMBackendConfig

        result = probe_llm_backend(LLMBackendConfig(provider="stub"))
        assert result.ok is True
        assert result.probed is False


@pytest.mark.django_db
class TestCheckBackendsSeedsTokenParam:
    """The token-param DB seeding moved from the probe into runner's _check_backends,
    so a server that needs max_completion_tokens only pays its first-attempt error
    once — at boot — not on every triage call."""

    def test_seeds_fallback_row_after_successful_retry(self) -> None:
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
        from franktheunicorn.core.models import LLMBackendFallback

        err = _make_bad_request("max_tokens is deprecated in favor of max_completion_tokens")
        mock_client = _make_client_mock([err, None])
        import openai

        mock_client.models.list.side_effect = openai.NotFoundError(
            message="not found", response=MagicMock(), body=None
        )
        cfg = OperatorConfig(
            github_username="testuser",
            llm_backends=[
                LLMBackendConfig(
                    provider="openai",
                    model="cortex-model",
                    base_url="https://snowhouse.example.com/api/v2/cortex/v1",
                    api_key_env="OPENAI_API_KEY",
                )
            ],
        )
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "fake-key"}),
            patch("openai.OpenAI", return_value=mock_client),
        ):
            from franktheunicorn.worker.runner import _check_backends

            _check_backends(cfg)

        row = LLMBackendFallback.objects.filter(
            provider="openai", model="cortex-model", base_url=cfg.llm_backends[0].base_url
        ).first()
        assert row is not None
        assert row.token_param == "max_completion_tokens"


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
