"""Tests for the setup_llm management command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from django.core.management import call_command

# Common mock to prevent actual API calls during model discovery.
_NO_DISCOVERY = patch(
    "franktheunicorn.core.management.commands.setup_llm.discover_models_verbose",
    return_value=([], ""),
)


@pytest.mark.django_db
class TestSetupLLMCommand:
    def test_stub_provider_generates_config(self, tmp_path: Path) -> None:
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "7",  # provider: skip/stub
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with patch("builtins.input", side_effect=inputs), _NO_DISCOVERY:
            call_command("setup_llm", output=str(output_path))

        assert output_path.exists()
        config = yaml.safe_load(output_path.read_text())
        assert config["github_username"] == "testuser"
        assert config["review_style"] == "direct"
        assert "llm_backends" not in config  # stub doesn't write backends

    def test_single_cloud_provider(self, tmp_path: Path) -> None:
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "holdenk",  # github_username
            "direct but kind",  # review_style
            "1",  # provider: claude only
            "claude-sonnet-4-20250514",  # model (from discovery fallback prompt)
            "0.3",  # temperature
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert isinstance(config["llm_backends"], list)
        assert len(config["llm_backends"]) == 1
        assert config["llm_backends"][0]["provider"] == "claude"
        assert config["llm_backends"][0]["model"] == "claude-sonnet-4-20250514"

    def test_multiple_providers(self, tmp_path: Path) -> None:
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "holdenk",  # github_username
            "direct but kind",  # review_style
            "1,4",  # providers: claude + ollama
            "claude-sonnet-4-20250514",  # claude model
            "0.3",  # claude temperature
            "http://localhost:11434",  # ollama base_url
            "qwen2.5-coder:14b",  # ollama model (from discovery fallback)
            "n",  # generate Docker Compose: no
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch(
                "franktheunicorn.review.backends.ollama_backend.recommend_local_model",
                return_value=("qwen2.5-coder:14b", "12GB VRAM available"),
            ),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert isinstance(config["llm_backends"], list)
        assert len(config["llm_backends"]) == 2
        assert config["llm_backends"][0]["provider"] == "claude"
        assert config["llm_backends"][1]["provider"] == "ollama"

    def test_ollama_provider_with_gpu_detection(self, tmp_path: Path) -> None:
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "4",  # provider: ollama
            "http://localhost:11434",  # base_url
            "qwen2.5-coder:14b",  # model (from discovery fallback)
            "n",  # generate Docker Compose: no
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch(
                "franktheunicorn.review.backends.ollama_backend.recommend_local_model",
                return_value=("qwen2.5-coder:14b", "12GB VRAM available"),
            ),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert config["llm_backends"][0]["provider"] == "ollama"
        assert config["llm_backends"][0]["model"] == "qwen2.5-coder:14b"

    def test_ollama_generates_compose_when_accepted(self, tmp_path: Path) -> None:
        """When user says 'y' to Docker Compose, compose.ollama.yaml is generated."""
        output_path = tmp_path / "operator.yaml"
        template_dir = tmp_path / "docker"
        template_dir.mkdir()
        template_path = template_dir / "compose.ollama.yaml.template"
        template_path.write_text(
            "services:\n  ollama:\n    image: ollama/ollama:latest\n"
            "  ollama-pull:\n    entrypoint: ['ollama', 'pull', '{{MODEL}}']\n"
        )
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "4",  # provider: ollama
            "http://localhost:11434",  # base_url
            "qwen2.5-coder:14b",  # model
            "y",  # generate Docker Compose: yes
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch(
                "franktheunicorn.review.backends.ollama_backend.recommend_local_model",
                return_value=("qwen2.5-coder:14b", "12GB VRAM available"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        compose_output = tmp_path / "compose.ollama.yaml"
        assert compose_output.exists()
        content = compose_output.read_text()
        assert "qwen2.5-coder:14b" in content
        assert "{{MODEL}}" not in content

    def test_ollama_skips_compose_when_declined(self, tmp_path: Path) -> None:
        """When user says 'n' to Docker Compose, no compose file is generated."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "4",  # provider: ollama
            "http://localhost:11434",  # base_url
            "qwen2.5-coder:14b",  # model
            "n",  # generate Docker Compose: no
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch(
                "franktheunicorn.review.backends.ollama_backend.recommend_local_model",
                return_value=("qwen2.5-coder:14b", "12GB VRAM available"),
            ),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        compose_output = tmp_path / "compose.ollama.yaml"
        assert not compose_output.exists()

    def test_coderabbit_enabled(self, tmp_path: Path) -> None:
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "7",  # provider: skip/stub
            "",  # projects: skip
            "y",  # coderabbit: yes
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch("shutil.which", return_value="/usr/bin/coderabbit"),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert config["coderabbit"]["enabled"] is True


@pytest.mark.django_db
class TestCredentialDetectionIntegration:
    """Tests for credential detection integration in the setup wizard."""

    def test_detects_anthropic_key_preselects_claude(self, tmp_path: Path) -> None:
        """When ANTHROPIC_API_KEY is in env, default choice should be '1'."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "",  # accept default provider choice (should be "1" from detection)
            "claude-sonnet-4-20250514",  # model
            "0.3",  # temperature
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-test123"}, clear=False),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert isinstance(config["llm_backends"], list)
        assert len(config["llm_backends"]) == 1
        assert config["llm_backends"][0]["provider"] == "claude"

    def test_detects_multiple_keys_preselects_both(self, tmp_path: Path) -> None:
        """When multiple tier-1 keys are in env, default is comma-separated."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "",  # accept default (should be "1,2")
            "claude-sonnet-4-20250514",  # claude model
            "0.3",  # claude temperature
            "gpt-4o",  # openai model
            "0.3",  # openai temperature
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict(
                "os.environ",
                {"ANTHROPIC_API_KEY": "sk-ant-test", "OPENAI_API_KEY": "sk-proj-test"},
                clear=False,
            ),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert len(config["llm_backends"]) == 2
        providers = {b["provider"] for b in config["llm_backends"]}
        assert providers == {"claude", "openai"}

    def test_no_keys_defaults_to_skip(self, tmp_path: Path) -> None:
        """When no LLM keys in env, default is '7' (skip)."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "",  # accept default (should be "7")
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        # Clear all known LLM env vars to ensure clean state.
        clean_env = {
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
            "GOOGLE_API_KEY": "",
        }
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", clean_env, clear=False),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert "llm_backends" not in config

    def test_tier2_detection_appears_in_menu(self, tmp_path: Path) -> None:
        """When GROQ_API_KEY is detected, it appears as a dynamic menu entry."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "8",  # select detected groq backend (dynamic menu entry)
            "https://api.groq.com/openai/v1",  # base_url
            "llama-3.3-70b-versatile",  # model
            "0.3",  # temperature
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        clean_env = {
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
            "GOOGLE_API_KEY": "",
            "GROQ_API_KEY": "gsk_test123",
        }
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", clean_env, clear=False),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert len(config["llm_backends"]) == 1
        backend = config["llm_backends"][0]
        assert backend["provider"] == "openai"
        assert backend["api_key_env"] == "GROQ_API_KEY"
        assert backend["base_url"] == "https://api.groq.com/openai/v1"


@pytest.mark.django_db
class TestModelDiscoveryIntegration:
    """Tests for model discovery integration in the setup wizard."""

    def test_model_discovery_shown_when_available(self, tmp_path: Path) -> None:
        """When discover_models returns models, they are shown in the menu."""
        from franktheunicorn.config.model_discovery import DiscoveredModel

        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "1",  # provider: claude
            "1",  # select first model from discovery menu
            "0.3",  # temperature
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        mock_models = [
            DiscoveredModel("claude-sonnet-4-20250514", "Claude Sonnet 4"),
            DiscoveredModel("claude-opus-4-20250514", "Claude Opus 4"),
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.discover_models_verbose",
                return_value=(mock_models, ""),
            ),
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert config["llm_backends"][0]["model"] == "claude-sonnet-4-20250514"

    def test_model_discovery_fallback_on_failure(self, tmp_path: Path) -> None:
        """When discover_models returns empty, user types model name directly."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "1",  # provider: claude
            "claude-sonnet-4-20250514",  # typed model name
            "0.3",  # temperature
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert config["llm_backends"][0]["model"] == "claude-sonnet-4-20250514"


@pytest.mark.django_db
class TestLlamaCppProvider:
    def test_llama_cpp_configures_as_openai_compatible(self, tmp_path: Path) -> None:
        """llama.cpp provider stores config as openai with base_url."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "5",  # provider: llama-cpp
            "http://localhost:8080/v1",  # server URL
            "my-model",  # model name
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch("shutil.which", return_value=None),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert len(config["llm_backends"]) == 1
        backend = config["llm_backends"][0]
        assert backend["provider"] == "openai"
        assert backend["base_url"] == "http://localhost:8080/v1"
        assert backend["model"] == "my-model"

    def test_llama_cpp_warns_when_not_installed(self, tmp_path: Path) -> None:
        """Shows warning when llama-server is not on PATH."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "5",  # provider: llama-cpp
            "http://localhost:8080/v1",  # server URL
            "my-model",  # model name
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch("shutil.which", return_value=None),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        # Config should still be generated despite the warning
        assert output_path.exists()


@pytest.mark.django_db
class TestVLLMProvider:
    def test_vllm_configures_as_openai_compatible(self, tmp_path: Path) -> None:
        """vLLM provider stores config as openai with base_url."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "6",  # provider: vllm
            "http://localhost:8081/v1",  # server URL
            "meta-llama/Llama-3-8b",  # model name
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch("shutil.which", return_value=None),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert len(config["llm_backends"]) == 1
        backend = config["llm_backends"][0]
        assert backend["provider"] == "openai"
        assert backend["base_url"] == "http://localhost:8081/v1"
        assert backend["model"] == "meta-llama/Llama-3-8b"


@pytest.mark.django_db
class TestCustomEndpointFallback:
    def test_custom_url_endpoint(self, tmp_path: Path) -> None:
        """When no provider chosen, user can enter a raw URL."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "99",  # invalid choice → skipped, no backends
            "https://my-llm.example.com/v1",  # custom endpoint URL
            "",  # no token needed
            "my-custom-model",  # model name
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        clean_env = {
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
            "GOOGLE_API_KEY": "",
        }
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", clean_env, clear=False),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert len(config["llm_backends"]) == 1
        backend = config["llm_backends"][0]
        assert backend["provider"] == "openai"
        assert backend["base_url"] == "https://my-llm.example.com/v1"
        assert backend["model"] == "my-custom-model"

    def test_custom_env_var_endpoint(self, tmp_path: Path) -> None:
        """When user enters an env var name, it resolves from environment."""
        output_path = tmp_path / "operator.yaml"
        # Use an env var name that won't trigger tier-3 endpoint detection
        # (no _URL/_ENDPOINT/_BASE suffix).
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "99",  # invalid choice → skipped
            "MY_LLM_SERVER",  # env var name (fallback prompt)
            "MY_LLM_TOKEN",  # env var for token
            "my-model",  # model name
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        custom_env = {
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
            "GOOGLE_API_KEY": "",
            "MY_LLM_SERVER": "https://custom.example.com/v1",
        }
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", custom_env, clear=False),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert len(config["llm_backends"]) == 1
        backend = config["llm_backends"][0]
        assert backend["base_url"] == "https://custom.example.com/v1"
        assert backend["api_key_env"] == "MY_LLM_TOKEN"

    def test_custom_raw_token(self, tmp_path: Path) -> None:
        """When user pastes a raw token (sk- prefix), it sets FRANK_LLM_API_KEY."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "99",  # invalid choice
            "https://api.example.com/v1",  # endpoint URL
            "sk-my-secret-token-value",  # raw token
            "my-model",  # model name
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        clean_env = {
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
            "GOOGLE_API_KEY": "",
        }
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", clean_env, clear=False),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        backend = config["llm_backends"][0]
        assert backend["api_key_env"] == "FRANK_LLM_API_KEY"

    def test_custom_skipped_when_enter_pressed(self, tmp_path: Path) -> None:
        """When user presses Enter at the endpoint prompt, no backend is added."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "99",  # invalid choice
            "",  # skip custom endpoint
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        clean_env = {
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
            "GOOGLE_API_KEY": "",
        }
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", clean_env, clear=False),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert "llm_backends" not in config

    def test_custom_env_var_not_found_uses_as_is(self, tmp_path: Path) -> None:
        """When env var name doesn't resolve, uses the name as the URL."""
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "99",  # invalid choice
            "NONEXISTENT_VAR",  # env var that doesn't exist
            "",  # no token
            "my-model",  # model name
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        clean_env = {
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
            "GOOGLE_API_KEY": "",
        }
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", clean_env, clear=False),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert config["llm_backends"][0]["base_url"] == "NONEXISTENT_VAR"
