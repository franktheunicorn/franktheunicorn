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
class TestDockerMode:
    """Tests for --docker mode: skip install checks, use service names, auto-generate compose."""

    def test_ollama_docker_mode_uses_service_url_and_generates_compose(
        self, tmp_path: Path
    ) -> None:
        """In --docker mode, Ollama uses Docker service URL and auto-generates compose."""
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
            "",  # model: accept recommended default
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_local_model",
                return_value=("qwen2.5-coder:14b", "12GB VRAM available"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path), docker=True)

        config = yaml.safe_load(output_path.read_text())
        assert config["llm_backends"][0]["provider"] == "ollama"
        assert config["llm_backends"][0]["base_url"] == "http://ollama:11434"
        assert config["llm_backends"][0]["model"] == "qwen2.5-coder:14b"

        # Compose override was auto-generated
        compose_output = tmp_path / "compose.ollama.yaml"
        assert compose_output.exists()
        content = compose_output.read_text()
        assert "qwen2.5-coder:14b" in content
        assert "{{MODEL}}" not in content

    def test_llama_cpp_docker_mode_uses_service_url_and_generates_compose(
        self, tmp_path: Path
    ) -> None:
        """In --docker mode, llama-cpp uses Docker service URL and auto-generates compose."""
        output_path = tmp_path / "operator.yaml"
        template_dir = tmp_path / "docker"
        template_dir.mkdir()
        template_path = template_dir / "compose.llama-cpp.yaml.template"
        template_path.write_text(
            "services:\n  llama-cpp:\n    image: ghcr.io/ggerganov/llama.cpp:server\n"
            "    command: -m /models/{{MODEL}} --host 0.0.0.0 --port 8080\n"
        )
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "5",  # provider: llama-cpp
            "my-model.gguf",  # GGUF filename
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_gguf_model",
                return_value=("Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf", "12GB VRAM available"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path), docker=True)

        config = yaml.safe_load(output_path.read_text())
        assert config["llm_backends"][0]["provider"] == "openai"
        assert config["llm_backends"][0]["base_url"] == "http://llama-cpp:8080/v1"
        assert config["llm_backends"][0]["model"] == "my-model.gguf"

        # Compose override was auto-generated
        compose_output = tmp_path / "compose.llama-cpp.yaml"
        assert compose_output.exists()
        content = compose_output.read_text()
        assert "my-model.gguf" in content
        assert "{{MODEL}}" not in content

    def test_vllm_docker_mode_uses_service_url_and_generates_compose(self, tmp_path: Path) -> None:
        """In --docker mode, vLLM uses Docker service URL and auto-generates compose."""
        output_path = tmp_path / "operator.yaml"
        template_dir = tmp_path / "docker"
        template_dir.mkdir()
        template_path = template_dir / "compose.vllm.yaml.template"
        template_path.write_text(
            "services:\n  vllm:\n    image: vllm/vllm-openai:latest\n"
            '    command: ["--model", "{{MODEL}}", "--host", "0.0.0.0"]\n'
        )
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "6",  # provider: vllm
            "Qwen/Qwen2.5-Coder-14B",  # HuggingFace model name
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_hf_model",
                return_value=("Qwen/Qwen2.5-Coder-7B-Instruct", "12GB VRAM available"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path), docker=True)

        config = yaml.safe_load(output_path.read_text())
        assert config["llm_backends"][0]["provider"] == "openai"
        assert config["llm_backends"][0]["base_url"] == "http://vllm:8000/v1"
        assert config["llm_backends"][0]["model"] == "Qwen/Qwen2.5-Coder-14B"

        # Compose override was auto-generated
        compose_output = tmp_path / "compose.vllm.yaml"
        assert compose_output.exists()
        content = compose_output.read_text()
        assert "Qwen/Qwen2.5-Coder-14B" in content
        assert "{{MODEL}}" not in content

    def test_ollama_docker_mode_does_not_check_which(self, tmp_path: Path) -> None:
        """In --docker mode, shutil.which is never called for ollama."""
        output_path = tmp_path / "operator.yaml"
        template_dir = tmp_path / "docker"
        template_dir.mkdir()
        (template_dir / "compose.ollama.yaml.template").write_text(
            "services:\n  ollama-pull:\n    entrypoint: ['ollama', 'pull', '{{MODEL}}']\n"
        )
        inputs = [
            "testuser",
            "direct",
            "4",  # ollama
            "",  # accept default model
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        # Patch shutil.which at the setup_llm module level — this only covers
        # the direct which("ollama") call in _configure_ollama, not the one
        # inside recommend_local_model (which we mock separately).
        mock_which = patch(
            "franktheunicorn.core.management.commands.setup_llm.shutil.which",
            side_effect=AssertionError("should not be called"),
        )
        with (
            patch("builtins.input", side_effect=inputs),
            mock_which,
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_local_model",
                return_value=("qwen2.5-coder:7b", "CPU mode"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            _NO_DISCOVERY,
        ):
            # Should not raise — shutil.which is bypassed in docker mode
            call_command("setup_llm", output=str(output_path), docker=True)

        assert output_path.exists()

    def test_compose_template_path_falls_back_to_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When BASE_DIR/docker doesn't exist (pip-installed in Docker), fall back to cwd.

        Simulates the Docker container scenario where ``BASE_DIR`` resolves to
        the site-packages location (``/usr/local/lib/python3.12``) but the
        templates live at ``/app/docker/`` (the working directory).
        """
        # BASE_DIR points to a directory that has no `docker/` subdir.
        fake_base_dir = tmp_path / "fake-site-packages"
        fake_base_dir.mkdir()

        # The "real" project root (cwd) has the templates.
        project_root = tmp_path / "app"
        project_root.mkdir()
        template_dir = project_root / "docker"
        template_dir.mkdir()
        (template_dir / "compose.ollama.yaml.template").write_text(
            "services:\n  ollama-pull:\n    entrypoint: ['ollama', 'pull', '{{MODEL}}']\n"
        )

        output_path = project_root / "operator.yaml"
        monkeypatch.chdir(project_root)

        inputs = [
            "testuser",
            "direct",
            "4",  # ollama
            "",  # accept default model
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_local_model",
                return_value=("qwen2.5-coder:7b", "test"),
            ),
            patch("django.conf.settings.BASE_DIR", str(fake_base_dir)),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path), docker=True)

        # Compose was generated relative to the cwd fallback, not BASE_DIR.
        compose_output = project_root / "compose.ollama.yaml"
        assert compose_output.exists()
        content = compose_output.read_text()
        assert "qwen2.5-coder:7b" in content
        assert "{{MODEL}}" not in content

        # Nothing was written under the (broken) BASE_DIR.
        assert not (fake_base_dir / "compose.ollama.yaml").exists()


@pytest.mark.django_db
class TestProjectRootResolution:
    """Direct tests for Command._get_project_root() fallback behaviour."""

    def test_returns_base_dir_when_docker_subdir_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from franktheunicorn.core.management.commands.setup_llm import Command

        base_dir = tmp_path / "real-base"
        (base_dir / "docker").mkdir(parents=True)
        cwd_with_docker = tmp_path / "other"
        (cwd_with_docker / "docker").mkdir(parents=True)
        monkeypatch.chdir(cwd_with_docker)

        cmd = Command()
        with patch("django.conf.settings.BASE_DIR", str(base_dir)):
            assert cmd._get_project_root() == base_dir

    def test_falls_back_to_cwd_when_base_dir_has_no_docker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from franktheunicorn.core.management.commands.setup_llm import Command

        base_dir = tmp_path / "fake-site-packages"
        base_dir.mkdir()
        cwd = tmp_path / "app"
        (cwd / "docker").mkdir(parents=True)
        monkeypatch.chdir(cwd)

        cmd = Command()
        with patch("django.conf.settings.BASE_DIR", str(base_dir)):
            assert cmd._get_project_root() == cwd

    def test_returns_base_dir_when_neither_has_docker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If neither BASE_DIR nor cwd has a docker/ subdir, return BASE_DIR.

        The caller will then hit the "template not found" branch and print
        a warning — better to surface a broken absolute path than a cwd one.
        """
        from franktheunicorn.core.management.commands.setup_llm import Command

        base_dir = tmp_path / "base"
        base_dir.mkdir()
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        cmd = Command()
        with patch("django.conf.settings.BASE_DIR", str(base_dir)):
            assert cmd._get_project_root() == base_dir


@pytest.mark.django_db
class TestMissingTemplateWarnings:
    """Cover 'template not found' and OSError branches for all three compose generators."""

    def test_ollama_docker_mode_missing_template_warns(self, tmp_path: Path) -> None:
        """When the ollama template is missing, generation warns and doesn't write output."""
        output_path = tmp_path / "operator.yaml"
        (tmp_path / "docker").mkdir()  # empty docker/ — template missing
        inputs = [
            "testuser",
            "direct",
            "4",  # ollama
            "",  # accept recommended default
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_local_model",
                return_value=("qwen2.5-coder:3b", "test"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path), docker=True)

        assert output_path.exists()
        assert not (tmp_path / "compose.ollama.yaml").exists()

    def test_llama_cpp_docker_mode_missing_template_warns(self, tmp_path: Path) -> None:
        """When the llama-cpp template is missing, generation warns and doesn't write output."""
        output_path = tmp_path / "operator.yaml"
        # Deliberately don't create the template file.
        (tmp_path / "docker").mkdir()
        inputs = [
            "testuser",
            "direct",
            "5",  # llama-cpp
            "my-model.gguf",
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_gguf_model",
                return_value=("Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf", "test"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path), docker=True)

        assert output_path.exists()
        # No compose override was generated because the template was missing.
        assert not (tmp_path / "compose.llama-cpp.yaml").exists()

    def test_vllm_docker_mode_missing_template_warns(self, tmp_path: Path) -> None:
        """When the vLLM template is missing, generation warns and doesn't write output."""
        output_path = tmp_path / "operator.yaml"
        (tmp_path / "docker").mkdir()
        inputs = [
            "testuser",
            "direct",
            "6",  # vllm
            "Qwen/Qwen2.5-Coder-7B-Instruct",
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_hf_model",
                return_value=("Qwen/Qwen2.5-Coder-3B-Instruct", "test"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path), docker=True)

        assert output_path.exists()
        assert not (tmp_path / "compose.vllm.yaml").exists()


@pytest.mark.django_db
class TestComposeGeneratorOSError:
    """Cover OSError branches in the compose generators.

    Triggers the failure by patching Path.write_text to raise — simulates
    a read-only filesystem or permission error during write.
    """

    @staticmethod
    def _make_write_text_fail(template_path: Path) -> object:
        """Return a patched write_text that only fails for compose output files.

        Reading the template must still succeed so we reach the except branch.
        """
        real_write_text = Path.write_text

        def fake_write_text(
            self: Path,
            data: str,
            encoding: str | None = None,
            errors: str | None = None,
            newline: str | None = None,
        ) -> int:
            if self.name.startswith("compose.") and self.name.endswith(".yaml"):
                raise OSError("read-only filesystem")
            return real_write_text(self, data, encoding=encoding, errors=errors, newline=newline)

        return fake_write_text

    def _write_minimal_templates(self, tmp_path: Path) -> None:
        template_dir = tmp_path / "docker"
        template_dir.mkdir()
        for name in (
            "compose.ollama.yaml.template",
            "compose.llama-cpp.yaml.template",
            "compose.vllm.yaml.template",
        ):
            (template_dir / name).write_text("services: {}\n# model={{MODEL}}\n")

    def test_ollama_oserror_is_swallowed(self, tmp_path: Path) -> None:
        self._write_minimal_templates(tmp_path)
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",
            "direct",
            "4",  # ollama
            "",  # accept default
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_local_model",
                return_value=("qwen2.5-coder:3b", "test"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            patch.object(Path, "write_text", self._make_write_text_fail(tmp_path)),
            _NO_DISCOVERY,
        ):
            # Should not raise — the OSError is caught and reported as a warning.
            call_command("setup_llm", output=str(output_path), docker=True)

    def test_llama_cpp_oserror_is_swallowed(self, tmp_path: Path) -> None:
        self._write_minimal_templates(tmp_path)
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",
            "direct",
            "5",  # llama-cpp
            "my-model.gguf",
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_gguf_model",
                return_value=("Qwen2.5-Coder-3B-Instruct-Q4_K_M.gguf", "test"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            patch.object(Path, "write_text", self._make_write_text_fail(tmp_path)),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path), docker=True)

    def test_vllm_oserror_is_swallowed(self, tmp_path: Path) -> None:
        self._write_minimal_templates(tmp_path)
        output_path = tmp_path / "operator.yaml"
        inputs = [
            "testuser",
            "direct",
            "6",  # vllm
            "Qwen/Qwen2.5-Coder-7B-Instruct",
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_hf_model",
                return_value=("Qwen/Qwen2.5-Coder-3B-Instruct", "test"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            patch.object(Path, "write_text", self._make_write_text_fail(tmp_path)),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path), docker=True)


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
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_gguf_model",
                return_value=("Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf", "test"),
            ),
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
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_gguf_model",
                return_value=("Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf", "test"),
            ),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        # Config should still be generated despite the warning
        assert output_path.exists()

    def test_llama_cpp_docker_mode_uses_recommended_default(self, tmp_path: Path) -> None:
        """In --docker mode, llama.cpp accepts the recommended GGUF default on Enter."""
        output_path = tmp_path / "operator.yaml"
        template_dir = tmp_path / "docker"
        template_dir.mkdir()
        (template_dir / "compose.llama-cpp.yaml.template").write_text(
            "services:\n  llama-cpp:\n    command: -m /models/{{MODEL}}\n"
        )
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "5",  # provider: llama-cpp
            "",  # accept recommended default
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_gguf_model",
                return_value=("Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf", "12GB VRAM available"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path), docker=True)

        config = yaml.safe_load(output_path.read_text())
        backend = config["llm_backends"][0]
        assert backend["model"] == "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf"

        compose_output = tmp_path / "compose.llama-cpp.yaml"
        assert compose_output.exists()
        assert "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf" in compose_output.read_text()


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
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_hf_model",
                return_value=("Qwen/Qwen2.5-Coder-7B-Instruct", "test"),
            ),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path))

        config = yaml.safe_load(output_path.read_text())
        assert len(config["llm_backends"]) == 1
        backend = config["llm_backends"][0]
        assert backend["provider"] == "openai"
        assert backend["base_url"] == "http://localhost:8081/v1"
        assert backend["model"] == "meta-llama/Llama-3-8b"

    def test_vllm_docker_mode_uses_recommended_default(self, tmp_path: Path) -> None:
        """In --docker mode, vLLM accepts the recommended HF model default on Enter."""
        output_path = tmp_path / "operator.yaml"
        template_dir = tmp_path / "docker"
        template_dir.mkdir()
        (template_dir / "compose.vllm.yaml.template").write_text(
            'services:\n  vllm:\n    command: ["--model", "{{MODEL}}"]\n'
        )
        inputs = [
            "testuser",  # github_username
            "direct",  # review_style
            "6",  # provider: vllm
            "",  # accept recommended default
            "",  # projects: skip
            "n",  # coderabbit: no
        ]
        with (
            patch("builtins.input", side_effect=inputs),
            patch(
                "franktheunicorn.core.management.commands.setup_llm.recommend_hf_model",
                return_value=("Qwen/Qwen2.5-Coder-14B-Instruct", "12GB VRAM available"),
            ),
            patch("django.conf.settings.BASE_DIR", str(tmp_path)),
            _NO_DISCOVERY,
        ):
            call_command("setup_llm", output=str(output_path), docker=True)

        config = yaml.safe_load(output_path.read_text())
        backend = config["llm_backends"][0]
        assert backend["model"] == "Qwen/Qwen2.5-Coder-14B-Instruct"

        compose_output = tmp_path / "compose.vllm.yaml"
        assert compose_output.exists()
        assert "Qwen/Qwen2.5-Coder-14B-Instruct" in compose_output.read_text()


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
