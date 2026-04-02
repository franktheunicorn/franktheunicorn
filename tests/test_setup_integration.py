"""
End-to-end integration tests for the setup wizard.

Validates the full chain: wizard → YAML file → load config → backends
initialise → draft_review produces drafts. This is the "new person"
experience — run the wizard, get a working system.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from franktheunicorn.config.loader import load_operator_config
from franktheunicorn.core.models import PullRequest
from franktheunicorn.review.backends import get_backend
from franktheunicorn.review.drafter import draft_review


@pytest.mark.django_db
@pytest.mark.integration
class TestWizardEndToEnd:
    """Test that the setup wizard produces config that actually works."""

    def test_stub_wizard_generates_working_config(
        self,
        tmp_path: Path,
        db_pr: PullRequest,
        spark_project_config: Any,
    ) -> None:
        """Wizard with stub/skip → load config → draft_review works."""
        from django.core.management import call_command

        output_path = tmp_path / "operator.yaml"
        inputs = [
            "holdenk",  # github_username
            "direct but kind",  # review_style
            "5",  # provider: stub
            "n",  # coderabbit: no
        ]
        with patch("builtins.input", side_effect=inputs):
            call_command("setup_llm", output=str(output_path))

        # Step 1: Wizard wrote valid YAML
        assert output_path.exists()
        raw = yaml.safe_load(output_path.read_text())
        assert isinstance(raw, dict)

        # Step 2: Config loader can parse it
        config = load_operator_config(output_path)
        assert config.github_username == "holdenk"
        assert config.review_style == "direct but kind"
        assert config.llm_backends == []  # stub = no backends

        # Step 3: draft_review works with this config (falls back to stub)
        drafts = draft_review(db_pr, spark_project_config, config)
        assert len(drafts) > 0
        assert all(d.status == "pending" for d in drafts)
        assert all("agent" in d.sources for d in drafts)

    def test_single_backend_wizard_generates_working_config(
        self,
        tmp_path: Path,
        db_pr: PullRequest,
        spark_project_config: Any,
    ) -> None:
        """Wizard with claude → load config → backends init → drafts work."""
        from django.core.management import call_command

        output_path = tmp_path / "operator.yaml"
        inputs = [
            "holdenk",
            "direct but kind",
            "1",  # claude
            "claude-sonnet-4-20250514",  # model
            "0.3",  # temperature
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
        ):
            call_command("setup_llm", output=str(output_path))

        # Load and validate
        config = load_operator_config(output_path)
        assert len(config.llm_backends) == 1
        assert config.llm_backends[0].provider == "claude"
        assert config.llm_backends[0].model == "claude-sonnet-4-20250514"
        assert config.llm_backends[0].api_key_env == "ANTHROPIC_API_KEY"

        # Backend initialises without error
        backend = get_backend(config.llm_backends[0])
        assert type(backend).__name__ == "ClaudeBackend"

    def test_multi_backend_wizard_generates_working_config(
        self,
        tmp_path: Path,
        db_pr: PullRequest,
        spark_project_config: Any,
    ) -> None:
        """Wizard with claude+ollama → load config → both backends init."""
        from django.core.management import call_command

        output_path = tmp_path / "operator.yaml"
        inputs = [
            "holdenk",
            "direct but kind",
            "1,4",  # claude + ollama
            "claude-sonnet-4-20250514",  # claude model
            "0.3",  # claude temperature
            "qwen2.5-coder:14b",  # ollama model
            "http://localhost:11434",  # ollama base_url
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
        ):
            call_command("setup_llm", output=str(output_path))

        # Load and validate
        config = load_operator_config(output_path)
        assert len(config.llm_backends) == 2

        # Both backends initialise
        backends = [get_backend(bc) for bc in config.llm_backends]
        backend_names = [type(b).__name__ for b in backends]
        assert "ClaudeBackend" in backend_names
        assert "OllamaBackend" in backend_names

    def test_legacy_llm_field_yaml_loads_and_works(
        self,
        tmp_path: Path,
        db_pr: PullRequest,
        spark_project_config: Any,
    ) -> None:
        """Legacy ``llm:`` YAML (single backend) loads and produces drafts."""
        config_path = tmp_path / "operator.yaml"
        config_path.write_text(
            "github_username: holdenk\nreview_style: direct\nllm:\n  provider: stub\n"
        )

        config = load_operator_config(config_path)
        # Legacy field promoted to llm_backends
        assert len(config.llm_backends) == 1
        assert config.llm_backends[0].provider == "stub"

        drafts = draft_review(db_pr, spark_project_config, config)
        assert len(drafts) > 0

    def test_wizard_with_coderabbit_generates_valid_config(
        self,
        tmp_path: Path,
    ) -> None:
        """Wizard with CodeRabbit enabled → valid config with both sections."""
        from django.core.management import call_command

        output_path = tmp_path / "operator.yaml"
        inputs = [
            "holdenk",
            "direct but kind",
            "1",  # claude
            "claude-sonnet-4-20250514",
            "0.3",
            "y",  # coderabbit: yes
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
            patch("shutil.which", return_value="/usr/bin/coderabbit"),
        ):
            call_command("setup_llm", output=str(output_path))

        config = load_operator_config(output_path)
        assert len(config.llm_backends) == 1
        assert config.coderabbit.enabled is True
        assert config.coderabbit.cli_path == "coderabbit"


@pytest.mark.django_db
class TestConfigRoundTrip:
    """Test that configs written and re-read produce the same model."""

    def test_multi_backend_yaml_roundtrip(self, tmp_path: Path) -> None:
        """Write multi-backend config → read back → same values."""
        config_path = tmp_path / "operator.yaml"
        original = {
            "github_username": "holdenk",
            "review_style": "thorough",
            "llm_backends": [
                {
                    "provider": "claude",
                    "model": "claude-sonnet-4-20250514",
                    "api_key_env": "ANTHROPIC_API_KEY",
                    "temperature": 0.2,
                    "max_tokens": 8192,
                },
                {
                    "provider": "ollama",
                    "model": "qwen2.5-coder:32b",
                    "base_url": "http://gpu-box:11434",
                },
            ],
            "coderabbit": {"enabled": True, "cli_path": "/usr/local/bin/coderabbit"},
        }
        with config_path.open("w") as f:
            yaml.dump(original, f)

        config = load_operator_config(config_path)
        assert config.github_username == "holdenk"
        assert len(config.llm_backends) == 2
        assert config.llm_backends[0].provider == "claude"
        assert config.llm_backends[0].temperature == 0.2
        assert config.llm_backends[0].max_tokens == 8192
        assert config.llm_backends[1].provider == "ollama"
        assert config.llm_backends[1].base_url == "http://gpu-box:11434"
        assert config.coderabbit.enabled is True

    def test_empty_config_uses_defaults(self, tmp_path: Path) -> None:
        """Empty YAML file → valid config with all defaults."""
        config_path = tmp_path / "operator.yaml"
        config_path.write_text("")

        config = load_operator_config(config_path)
        assert config.github_username == ""
        assert config.llm_backends == []
        assert config.coderabbit.enabled is False

    def test_partial_backend_config_uses_defaults(self, tmp_path: Path) -> None:
        """Backend with only provider → other fields use defaults."""
        config_path = tmp_path / "operator.yaml"
        config_path.write_text("llm_backends:\n  - provider: openai\n")

        config = load_operator_config(config_path)
        assert len(config.llm_backends) == 1
        assert config.llm_backends[0].provider == "openai"
        assert config.llm_backends[0].model == ""  # default
        assert config.llm_backends[0].temperature == 0.3  # default
        assert config.llm_backends[0].max_tokens == 4096  # default

    def test_invalid_backend_provider_still_loads(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Unknown provider → config loads with warning, backend falls back to stub."""
        import logging

        config_path = tmp_path / "operator.yaml"
        config_path.write_text("llm_backends:\n  - provider: deepseek\n")

        with caplog.at_level(logging.WARNING):
            config = load_operator_config(config_path)

        assert len(config.llm_backends) == 1
        assert config.llm_backends[0].provider == "deepseek"
        assert "Unknown LLM provider" in caplog.text

        # Falls back to stub
        backend = get_backend(config.llm_backends[0])
        assert type(backend).__name__ == "StubBackend"
