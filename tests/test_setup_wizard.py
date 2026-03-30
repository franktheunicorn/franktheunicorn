"""Tests for the setup_llm management command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from django.core.management import call_command


@pytest.mark.django_db
class TestSetupLLMCommand:
    def test_stub_provider_generates_config(self, tmp_path: Path) -> None:
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "5",  # provider: stub
            "n",  # coderabbit: no
        ]
        with patch("builtins.input", side_effect=inputs):
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
            "claude-sonnet-4-20250514",  # model
            "0.3",  # temperature
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
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
            "qwen2.5-coder:14b",  # model
            "http://localhost:11434",  # base_url
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch("shutil.which", return_value="/usr/bin/ollama"),
            patch(
                "franktheunicorn.review.backends.ollama_backend.recommend_local_model",
                return_value=("qwen2.5-coder:14b", "12GB VRAM available"),
            ),
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert config["llm_backends"][0]["provider"] == "ollama"
        assert config["llm_backends"][0]["model"] == "qwen2.5-coder:14b"

    def test_coderabbit_enabled(self, tmp_path: Path) -> None:
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "5",  # provider: stub
            "y",  # coderabbit: yes
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch("shutil.which", return_value="/usr/bin/coderabbit"),
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert config["coderabbit"]["enabled"] is True
