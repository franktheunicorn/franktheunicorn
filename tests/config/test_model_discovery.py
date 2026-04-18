"""Tests for franktheunicorn.config.model_discovery."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.model_discovery import (
    DiscoveredModel,
    check_endpoint_reachability,
    discover_models,
    discover_models_verbose,
    format_model_menu,
    list_models_anthropic,
    list_models_gemini,
    list_models_ollama,
    list_models_openai,
)


@pytest.fixture(autouse=True)
def _mock_sdk_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure anthropic/openai/google/ollama modules exist for patching."""
    for mod_name, class_name in [
        ("anthropic", "Anthropic"),
        ("openai", "OpenAI"),
        ("ollama", "Client"),
    ]:
        if mod_name not in sys.modules:
            mod = ModuleType(mod_name)
            setattr(mod, class_name, MagicMock())
            monkeypatch.setitem(sys.modules, mod_name, mod)
        elif not hasattr(sys.modules[mod_name], class_name):
            setattr(sys.modules[mod_name], class_name, MagicMock())
    # google.genai needs a nested module structure
    if "google" not in sys.modules:
        google_mod = ModuleType("google")
        monkeypatch.setitem(sys.modules, "google", google_mod)
    if "google.genai" not in sys.modules:
        genai_mod = ModuleType("google.genai")
        genai_mod.Client = MagicMock()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    elif not hasattr(sys.modules["google.genai"], "Client"):
        sys.modules["google.genai"].Client = MagicMock()  # type: ignore[attr-defined]


class TestListModelsAnthropic:
    def test_returns_error_without_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            models, status = list_models_anthropic()
        assert models == []
        assert status == "error"

    def test_returns_models_on_success(self) -> None:
        mock_model = SimpleNamespace(
            id="claude-sonnet-4-20250514",
            display_name="Claude Sonnet 4",
        )
        mock_page = SimpleNamespace(data=[mock_model])
        mock_client = MagicMock()
        mock_client.models.list.return_value = mock_page

        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
            patch("anthropic.Anthropic", return_value=mock_client),
        ):
            models, status = list_models_anthropic()

        assert len(models) == 1
        assert models[0].model_id == "claude-sonnet-4-20250514"
        assert models[0].display_name == "Claude Sonnet 4"
        assert status == "ok"

    def test_returns_error_on_api_error(self) -> None:
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
            patch("anthropic.Anthropic", side_effect=Exception("API error")),
        ):
            models, status = list_models_anthropic()
        assert models == []
        assert status == "error"

    def test_returns_empty_when_api_returns_no_models(self) -> None:
        mock_client = MagicMock()
        mock_client.models.list.return_value = SimpleNamespace(data=[])
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
            patch("anthropic.Anthropic", return_value=mock_client),
        ):
            models, status = list_models_anthropic()
        assert models == []
        assert status == "empty"

    def test_custom_key_env(self) -> None:
        mock_client = MagicMock()
        mock_client.models.list.return_value = SimpleNamespace(data=[])
        with (
            patch.dict("os.environ", {"MY_CLAUDE_KEY": "sk-test"}, clear=True),
            patch("anthropic.Anthropic", return_value=mock_client),
        ):
            list_models_anthropic(api_key_env="MY_CLAUDE_KEY")
        mock_client.models.list.assert_called_once()


class TestListModelsOpenAI:
    def test_returns_error_without_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            models, status = list_models_openai()
        assert models == []
        assert status == "error"

    def test_returns_models_on_success(self) -> None:
        mock_model = SimpleNamespace(id="gpt-4o")
        mock_response = SimpleNamespace(data=[mock_model])
        mock_client = MagicMock()
        mock_client.models.list.return_value = mock_response

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}),
            patch("openai.OpenAI", return_value=mock_client),
        ):
            models, status = list_models_openai()

        assert len(models) == 1
        assert models[0].model_id == "gpt-4o"
        assert status == "ok"

    def test_returns_empty_when_api_returns_no_models(self) -> None:
        mock_client = MagicMock()
        mock_client.models.list.return_value = SimpleNamespace(data=[])
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}),
            patch("openai.OpenAI", return_value=mock_client),
        ):
            models, status = list_models_openai()
        assert models == []
        assert status == "empty"

    def test_passes_base_url(self) -> None:
        mock_client = MagicMock()
        mock_client.models.list.return_value = SimpleNamespace(data=[])

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}),
            patch("openai.OpenAI", return_value=mock_client) as mock_ctor,
        ):
            list_models_openai(base_url="https://api.groq.com/openai/v1")
        mock_ctor.assert_called_once_with(
            api_key="sk-test",
            base_url="https://api.groq.com/openai/v1",
        )

    def test_returns_error_on_error(self) -> None:
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}),
            patch("openai.OpenAI", side_effect=Exception("fail")),
        ):
            models, status = list_models_openai()
        assert models == []
        assert status == "error"


class TestListModelsGemini:
    def test_returns_error_without_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            models, status = list_models_gemini()
        assert models == []
        assert status == "error"

    def test_returns_models_on_success(self) -> None:
        mock_model = SimpleNamespace(
            name="models/gemini-2.5-flash",
            display_name="Gemini 2.5 Flash",
        )
        mock_client = MagicMock()
        mock_client.models.list.return_value = [mock_model]

        with (
            patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"}),
            patch("google.genai.Client", return_value=mock_client),
        ):
            models, status = list_models_gemini()

        assert len(models) == 1
        assert models[0].model_id == "gemini-2.5-flash"
        assert models[0].display_name == "Gemini 2.5 Flash"
        assert status == "ok"

    def test_strips_models_prefix(self) -> None:
        mock_model = SimpleNamespace(
            name="models/gemini-2.5-pro",
            display_name="Gemini 2.5 Pro",
        )
        mock_client = MagicMock()
        mock_client.models.list.return_value = [mock_model]

        with (
            patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"}),
            patch("google.genai.Client", return_value=mock_client),
        ):
            models, _status = list_models_gemini()

        assert models[0].model_id == "gemini-2.5-pro"

    def test_returns_empty_when_api_returns_no_models(self) -> None:
        mock_client = MagicMock()
        mock_client.models.list.return_value = []
        with (
            patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"}),
            patch("google.genai.Client", return_value=mock_client),
        ):
            models, status = list_models_gemini()
        assert models == []
        assert status == "empty"

    def test_returns_error_on_error(self) -> None:
        with (
            patch.dict("os.environ", {"GOOGLE_API_KEY": "test-key"}),
            patch("google.genai.Client", side_effect=Exception("fail")),
        ):
            models, status = list_models_gemini()
        assert models == []
        assert status == "error"


class TestListModelsOllama:
    def test_returns_models_on_success(self) -> None:
        mock_model = SimpleNamespace(model="qwen2.5-coder:14b")
        mock_client = MagicMock()
        mock_client.list.return_value = SimpleNamespace(models=[mock_model])

        with patch("ollama.Client", return_value=mock_client):
            models, status = list_models_ollama()

        assert len(models) == 1
        assert models[0].model_id == "qwen2.5-coder:14b"
        assert status == "ok"

    def test_returns_empty_when_no_local_models(self) -> None:
        mock_client = MagicMock()
        mock_client.list.return_value = SimpleNamespace(models=[])
        with patch("ollama.Client", return_value=mock_client):
            models, status = list_models_ollama()
        assert models == []
        assert status == "empty"

    def test_passes_base_url(self) -> None:
        mock_client = MagicMock()
        mock_client.list.return_value = SimpleNamespace(models=[])

        with patch("ollama.Client", return_value=mock_client) as mock_ctor:
            list_models_ollama(base_url="http://gpu-box:11434")
        mock_ctor.assert_called_once_with(host="http://gpu-box:11434")

    def test_returns_error_on_error(self) -> None:
        with patch("ollama.Client", side_effect=Exception("fail")):
            models, status = list_models_ollama()
        assert models == []
        assert status == "error"

    def test_default_base_url_passes_none(self) -> None:
        mock_client = MagicMock()
        mock_client.list.return_value = SimpleNamespace(models=[])

        with patch("ollama.Client", return_value=mock_client) as mock_ctor:
            list_models_ollama()
        mock_ctor.assert_called_once_with(host=None)


class TestDiscoverModels:
    def test_routes_to_anthropic(self) -> None:
        with patch(
            "franktheunicorn.config.model_discovery.list_models_anthropic",
            return_value=(
                [DiscoveredModel("claude-sonnet-4-20250514", "Claude Sonnet 4")],
                "ok",
            ),
        ) as mock:
            models, status = discover_models("claude")
        mock.assert_called_once_with(api_key_env="ANTHROPIC_API_KEY")
        assert len(models) == 1
        assert status == "ok"

    def test_routes_to_openai(self) -> None:
        with patch(
            "franktheunicorn.config.model_discovery.list_models_openai",
            return_value=([], "error"),
        ) as mock:
            discover_models("openai", base_url="https://api.example.com")
        mock.assert_called_once_with(
            api_key_env="OPENAI_API_KEY", base_url="https://api.example.com"
        )

    def test_routes_to_gemini(self) -> None:
        with patch(
            "franktheunicorn.config.model_discovery.list_models_gemini",
            return_value=([], "error"),
        ) as mock:
            discover_models("gemini")
        mock.assert_called_once_with(api_key_env="GOOGLE_API_KEY")

    def test_routes_to_ollama(self) -> None:
        with patch(
            "franktheunicorn.config.model_discovery.list_models_ollama",
            return_value=([], "error"),
        ) as mock:
            discover_models("ollama", base_url="http://localhost:11434")
        mock.assert_called_once_with(base_url="http://localhost:11434")

    def test_unknown_provider_returns_error(self) -> None:
        models, status = discover_models("unknown-provider")
        assert models == []
        assert status == "error"

    def test_custom_api_key_env(self) -> None:
        with patch(
            "franktheunicorn.config.model_discovery.list_models_anthropic",
            return_value=([], "error"),
        ) as mock:
            discover_models("claude", api_key_env="MY_KEY")
        mock.assert_called_once_with(api_key_env="MY_KEY")


class TestFormatModelMenu:
    def test_empty_models(self) -> None:
        assert format_model_menu([]) == ""

    def test_single_model(self) -> None:
        models = [DiscoveredModel("gpt-4o", "gpt-4o")]
        result = format_model_menu(models)
        assert "1." in result
        assert "gpt-4o" in result

    def test_display_name_shown_when_different(self) -> None:
        models = [DiscoveredModel("claude-sonnet-4-20250514", "Claude Sonnet 4")]
        result = format_model_menu(models)
        assert "Claude Sonnet 4" in result
        assert "claude-sonnet-4-20250514" in result

    def test_truncation_at_max_display(self) -> None:
        models = [DiscoveredModel(f"model-{i}", f"model-{i}") for i in range(25)]
        result = format_model_menu(models, max_display=10)
        assert "10." in result
        assert "11." not in result
        assert "and 15 more" in result

    def test_no_truncation_under_max(self) -> None:
        models = [DiscoveredModel(f"model-{i}", f"model-{i}") for i in range(3)]
        result = format_model_menu(models, max_display=10)
        assert "more" not in result


class TestCheckEndpointReachability:
    def test_resolvable_host_returns_empty(self) -> None:
        # localhost should always resolve
        assert check_endpoint_reachability("http://localhost:8080/v1") == ""

    def test_unresolvable_host_returns_diagnostic(self) -> None:
        result = check_endpoint_reachability("https://this-host-does-not-exist-abc123.example.com")
        assert "Could not resolve hostname" in result
        assert "this-host-does-not-exist-abc123.example.com" in result

    def test_missing_hostname_returns_diagnostic(self) -> None:
        result = check_endpoint_reachability("not-a-url")
        assert "Could not parse hostname" in result

    def test_empty_url_returns_diagnostic(self) -> None:
        result = check_endpoint_reachability("")
        assert result != ""

    def test_network_error_returns_diagnostic(self) -> None:
        with patch("socket.getaddrinfo", side_effect=OSError("Network unreachable")):
            result = check_endpoint_reachability("https://api.example.com/v1")
        assert "Network error" in result


class TestDiscoverModelsVerbose:
    def test_returns_models_on_success(self) -> None:
        expected = [DiscoveredModel("gpt-4o", "gpt-4o")]
        with patch(
            "franktheunicorn.config.model_discovery.discover_models",
            return_value=(expected, "ok"),
        ):
            models, diagnostic = discover_models_verbose("openai")
        assert models == expected
        assert diagnostic == ""

    def test_returns_diagnostic_on_error(self) -> None:
        with patch(
            "franktheunicorn.config.model_discovery.discover_models",
            return_value=([], "error"),
        ):
            models, diagnostic = discover_models_verbose(
                "openai",
                api_key_env="OPENAI_API_KEY",
                base_url="https://api.openai.com/v1",
            )
        assert models == []
        assert "Attempted: GET" in diagnostic

    def test_empty_status_shows_listing_failed(self) -> None:
        with patch(
            "franktheunicorn.config.model_discovery.discover_models",
            return_value=([], "empty"),
        ):
            models, diagnostic = discover_models_verbose("claude")
        assert models == []
        assert "Listing models failed" in diagnostic
        assert "API returned no models" in diagnostic

    def test_dns_failure_diagnostic(self) -> None:
        with (
            patch(
                "franktheunicorn.config.model_discovery.discover_models",
                return_value=([], "error"),
            ),
            patch(
                "franktheunicorn.config.model_discovery.check_endpoint_reachability",
                return_value="Could not resolve hostname 'bad.host'",
            ),
        ):
            _models, diagnostic = discover_models_verbose(
                "openai",
                base_url="https://bad.host/v1",
            )
        assert "Could not resolve" in diagnostic

    def test_reachable_but_empty_key_diagnostic(self) -> None:
        with (
            patch(
                "franktheunicorn.config.model_discovery.discover_models",
                return_value=([], "error"),
            ),
            patch(
                "franktheunicorn.config.model_discovery.check_endpoint_reachability",
                return_value="",
            ),
            patch.dict("os.environ", {}, clear=True),
        ):
            _models, diagnostic = discover_models_verbose(
                "openai",
                api_key_env="OPENAI_API_KEY",
                base_url="https://api.openai.com/v1",
            )
        assert "is empty" in diagnostic

    def test_unknown_provider_no_base_url(self) -> None:
        with patch(
            "franktheunicorn.config.model_discovery.discover_models",
            return_value=([], "error"),
        ):
            _models, diagnostic = discover_models_verbose("unknown-thing")
        assert _models == []
        assert "Unknown provider" in diagnostic

    def test_uses_default_base_url_for_known_provider(self) -> None:
        with (
            patch(
                "franktheunicorn.config.model_discovery.discover_models",
                return_value=([], "error"),
            ),
            patch(
                "franktheunicorn.config.model_discovery.check_endpoint_reachability",
                return_value="",
            ),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
        ):
            _models, diagnostic = discover_models_verbose(
                "claude",
                api_key_env="ANTHROPIC_API_KEY",
            )
        assert "api.anthropic.com" in diagnostic
