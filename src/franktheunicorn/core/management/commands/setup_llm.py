"""Interactive setup wizard for LLM backend configuration.

Usage::

    python manage.py setup_llm
    python manage.py setup_llm --output config/active/operator.yaml
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml
from django.core.management.base import BaseCommand

from franktheunicorn.config.credential_detection import (
    DynamicMenuEntry,
    build_dynamic_menu_entries,
    detect_llm_credentials,
    format_detections,
    suggest_provider_choices,
)
from franktheunicorn.config.model_discovery import (
    discover_models_verbose,
    format_model_menu,
)
from franktheunicorn.review.backends.ollama_backend import (
    recommend_gguf_model,
    recommend_hf_model,
    recommend_local_model,
)

# (provider_id, label, default_model, api_key_env)
_PROVIDERS: dict[str, tuple[str, str, str, str]] = {
    "1": ("claude", "Anthropic Claude", "claude-sonnet-4-20250514", "ANTHROPIC_API_KEY"),
    "2": ("openai", "OpenAI", "gpt-4o", "OPENAI_API_KEY"),
    "3": ("gemini", "Google Gemini", "gemini-2.5-flash", "GOOGLE_API_KEY"),
    "4": ("ollama", "Local model (Ollama)", "qwen2.5-coder:14b", ""),
    "5": ("llama-cpp", "Local model (llama.cpp)", "", ""),
    "6": ("vllm", "Local model (vLLM)", "", ""),
}


class Command(BaseCommand):
    help = "Interactive setup wizard for LLM backend and CodeRabbit configuration."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument(
            "--output",
            default="",
            help="Path to write operator.yaml (default: config/active/operator.yaml)",
        )
        parser.add_argument(
            "--docker",
            action="store_true",
            help="Docker mode: skip install checks, use container service names, auto-generate compose overrides.",
        )

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        self._docker_mode: bool = options.get("docker", False)
        self.stdout.write(self.style.SUCCESS("\n=== franktheunicorn LLM Setup Wizard ===\n"))

        # Resolve output path early so we can read existing config for defaults.
        output_path_str = options.get("output") or ""
        if not output_path_str:
            import django.conf

            base = Path(django.conf.settings.BASE_DIR)
            output_path_str = str(base / "config" / "active" / "operator.yaml")
        self._output_path = Path(output_path_str)

        # Read existing config so returning users keep their previous answers.
        existing_config: dict[str, object] = {}
        if self._output_path.exists():
            with self._output_path.open(encoding="utf-8") as f:
                existing_config = yaml.safe_load(f) or {}

        config: dict[str, object] = {}

        # --- GitHub username ---
        existing_username = str(existing_config.get("github_username", ""))
        if not existing_username:
            token = os.environ.get("FRANK_GITHUB_TOKEN", "")
            if token:
                from franktheunicorn.github.client import infer_github_username

                inferred = infer_github_username(token)
                if inferred:
                    existing_username = inferred
                    self.stdout.write(
                        self.style.SUCCESS(f"  Auto-detected GitHub username: {inferred}")
                    )

        config["github_username"] = self._ask(
            "GitHub username (for scoring — leave blank to skip): ",
            default=existing_username,
        )

        # --- Review style ---
        config["review_style"] = self._ask(
            "Review style/tone (e.g. 'direct but kind', 'thorough and formal'): ",
            default=str(existing_config.get("review_style", "direct but kind")),
        )

        # --- Detect credentials from environment ---
        detections = detect_llm_credentials()
        if detections:
            self.stdout.write("\n")
            self.stdout.write(format_detections(detections))

        default_choice = suggest_provider_choices(detections)

        # Build dynamic menu entries from Tier 2/3 detections.
        dynamic_entries = build_dynamic_menu_entries(detections)
        dynamic_lookup = {e.key: e for e in dynamic_entries}

        # --- LLM providers (multiple) ---
        self.stdout.write("\nSelect LLM providers for code review (you can enable multiple):\n")
        for key, (_, label, _, _) in _PROVIDERS.items():
            self.stdout.write(f"  {key}. {label}\n")
        self.stdout.write("  7. Skip — use stub/demo mode\n")
        if dynamic_entries:
            self.stdout.write("  --- Detected backends ---\n")
            for entry in dynamic_entries:
                source = f" (from {entry.base_url_env})" if entry.base_url_env else ""
                self.stdout.write(f"  {entry.key}. {entry.label}{source} (detected)\n")

        choices_raw = self._ask(
            "Enter choices (comma-separated, e.g. '1,4' for Claude + Ollama): ",
            default=default_choice,
        )
        chosen_keys = [c.strip() for c in choices_raw.split(",")]

        llm_backends: list[dict[str, object]] = []
        env_vars_needed: list[str] = []

        skipped = False
        for key in chosen_keys:
            if key == "7":
                skipped = True
                continue
            if key in dynamic_lookup:
                entry = dynamic_lookup[key]
                self.stdout.write(f"\n--- Configuring {entry.label} (detected) ---\n")
                llm_config = self._configure_detected_backend(entry)
                llm_backends.append(llm_config)
                if entry.api_key_env:
                    env_vars_needed.append(entry.api_key_env)
                continue
            if key not in _PROVIDERS:
                self.stdout.write(self.style.WARNING(f"  Skipping unknown choice '{key}'\n"))
                continue
            provider, provider_label, _default_model, api_key_env = _PROVIDERS[key]
            self.stdout.write(f"\n--- Configuring {provider_label} ---\n")

            llm_config_dict: dict[str, object] = {"provider": provider}

            if api_key_env:
                llm_config_dict = self._configure_cloud_provider(provider, llm_config_dict)
                env_vars_needed.append(api_key_env)
            elif provider == "ollama":
                llm_config_dict = self._configure_ollama(llm_config_dict)
            elif provider == "llama-cpp":
                llm_config_dict = self._configure_llama_cpp(llm_config_dict)
            elif provider == "vllm":
                llm_config_dict = self._configure_vllm(llm_config_dict)

            llm_backends.append(llm_config_dict)

        # --- Fallback: custom endpoint prompt when nothing configured ---
        if not llm_backends and not skipped:
            llm_backends = self._configure_custom_endpoint(llm_backends, env_vars_needed)

        if llm_backends:
            config["llm_backends"] = llm_backends

        # --- Initial projects ---
        output_path_obj = self._output_path
        self._configure_initial_projects(output_path_obj)

        # --- CodeRabbit ---
        cr_config = self._configure_coderabbit()
        if cr_config:
            config["coderabbit"] = cr_config

        self.stdout.write(f"\nConfig will be written to: {output_path_obj}\n")

        # Merge with existing config so we don't clobber fields managed
        # elsewhere (e.g. mock_mode, projects_dir, email).
        output_path_obj.parent.mkdir(parents=True, exist_ok=True)
        # Re-read in case another process updated the file since we started.
        existing: dict[str, object] = {}
        if output_path_obj.exists():
            with output_path_obj.open(encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}
        existing.update(config)
        with output_path_obj.open("w", encoding="utf-8") as f:
            yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

        self.stdout.write(self.style.SUCCESS(f"\nConfig saved to {output_path_obj}"))

        # Only show env var reminders for keys not already set.
        missing_vars = [v for v in env_vars_needed if not os.environ.get(v)]
        if missing_vars:
            self.stdout.write("\nStill needed (set in .env or your shell):\n")
            for env_var in missing_vars:
                self.stdout.write(f"  export {env_var}=<your-api-key>\n")

        self.stdout.write(
            "\nRun the worker with:\n"
            "  python manage.py runserver   # dashboard\n"
            "  python -m franktheunicorn.worker.runner  # worker\n\n"
        )

    def _ask(self, prompt: str, default: str = "") -> str:
        """Prompt for input with a default value."""
        if default:
            prompt = f"{prompt}[{default}] "
        try:
            value = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            self.stdout.write("\n")
            value = ""
        return value or default

    def _save_to_dotenv(self, key: str, value: str) -> None:
        """Append or update a key=value pair in .env."""
        import django.conf

        env_path = Path(django.conf.settings.BASE_DIR) / ".env"
        lines: list[str] = []
        if env_path.exists():
            lines = [
                line
                for line in env_path.read_text(encoding="utf-8").splitlines()
                if not line.startswith(f"{key}=")
            ]
        lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"  Saved {key} to .env\n"))

    def _discover_and_choose_model(
        self,
        provider: str,
        default_model: str,
        api_key_env: str = "",
        base_url: str = "",
    ) -> str:
        """Try to list models from the API and let the user pick one."""
        self.stdout.write("\n  Discovering available models...\n")
        models, diagnostic = discover_models_verbose(
            provider=provider,
            api_key_env=api_key_env,
            base_url=base_url,
        )
        if not models:
            if diagnostic:
                for line in diagnostic.splitlines():
                    self.stdout.write(self.style.WARNING(f"  {line}\n"))
            else:
                self.stdout.write("  Could not list models (SDK missing or API error).\n")
            return self._ask("  Model name: ", default=default_model)

        menu = format_model_menu(models)
        self.stdout.write(f"  Available models:\n{menu}\n\n")
        self.stdout.write("  Enter a number to select, or type a model name directly.\n")
        choice = self._ask("  Model: ", default=default_model)

        # If the user entered a number, resolve it.
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                return models[idx].model_id
        except ValueError:
            pass  # Non-numeric input treated as a direct model name

        return choice

    def _configure_cloud_provider(
        self,
        provider: str,
        llm_config: dict[str, object],
    ) -> dict[str, object]:
        """Configure a cloud LLM provider (Claude/OpenAI/Gemini)."""
        # Look up provider info from the consolidated dict.
        _prov = next(p for p in _PROVIDERS.values() if p[0] == provider)
        env_var = _prov[3]
        default_model = _prov[2]

        # Check if key is already in environment.
        existing_key = os.environ.get(env_var, "")
        if existing_key:
            self.stdout.write(self.style.SUCCESS(f"  Found {env_var} in environment.\n"))
        else:
            key_input = self._ask(f"  {env_var} (paste key, or Enter to skip): ")
            if key_input:
                os.environ[env_var] = key_input
                self._save_to_dotenv(env_var, key_input)
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Skipped. Set {env_var} in .env before using this provider.\n"
                    )
                )

        llm_config["api_key_env"] = env_var

        # Discover models from the API and let the user pick.
        model = self._discover_and_choose_model(
            provider=provider,
            default_model=default_model,
            api_key_env=env_var,
        )
        llm_config["model"] = model

        temp = self._ask("  Temperature (0.0-2.0): ", default="0.3")
        try:
            llm_config["temperature"] = float(temp)
        except ValueError:
            llm_config["temperature"] = 0.3

        return llm_config

    def _configure_ollama(self, llm_config: dict[str, object]) -> dict[str, object]:
        """Configure local Ollama backend with auto-detected model recommendation."""
        if not self._docker_mode and not shutil.which("ollama"):
            self.stdout.write(
                self.style.WARNING(
                    "\n  Ollama not found on PATH.\n  Install from: https://ollama.com/download\n"
                )
            )

        recommended_model, reason = recommend_local_model()
        self.stdout.write(f"\n  Hardware detection: {reason}\n")
        self.stdout.write(f"  Recommended model: {recommended_model}\n")

        if self._docker_mode:
            base_url = "http://ollama:11434"
            self.stdout.write(f"  Using Docker service URL: {base_url}\n")
        else:
            base_url = self._ask("  Ollama server URL: ", default="http://localhost:11434")
        llm_config["base_url"] = base_url

        if self._docker_mode:
            # Server isn't running yet during setup; use recommended model directly.
            model = self._ask("  Model name: ", default=recommended_model)
        else:
            model = self._discover_and_choose_model(
                provider="ollama",
                default_model=recommended_model,
                base_url=base_url,
            )
        llm_config["model"] = model

        if self._docker_mode:
            self._generate_ollama_compose(model)
            self.stdout.write("  Model will be pulled automatically by Docker Compose.\n")
        else:
            self.stdout.write(f"\n  To download the model, run:\n    ollama pull {model}\n")
            generate = self._ask(
                "  Generate Docker Compose override for Ollama? (y/N): ", default="n"
            )
            if generate.lower() in ("y", "yes"):
                self._generate_ollama_compose(model)

        return llm_config

    def _get_project_root(self) -> Path:
        """Return the project root directory for finding docker templates.

        In Docker, the package is pip-installed so ``BASE_DIR`` resolves to the
        site-packages location (e.g. ``/usr/local/lib/python3.12``) rather than
        ``/app``.  When ``BASE_DIR / "docker"`` does not exist, fall back to the
        current working directory, which the Dockerfile sets to ``/app``.
        """
        import django.conf

        base_dir = Path(django.conf.settings.BASE_DIR)
        if (base_dir / "docker").is_dir():
            return base_dir
        cwd = Path.cwd()
        if (cwd / "docker").is_dir():
            return cwd
        return base_dir

    def _generate_ollama_compose(self, model: str) -> None:
        """Generate compose.ollama.yaml from the template with the chosen model."""
        base_dir = self._get_project_root()
        template_path = base_dir / "docker" / "compose.ollama.yaml.template"
        output_path = base_dir / "compose.ollama.yaml"

        if not template_path.exists():
            self.stdout.write(self.style.WARNING(f"\n  Template not found: {template_path}\n"))
            return

        try:
            content = template_path.read_text(encoding="utf-8")
            content = content.replace("{{MODEL}}", model)
            output_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            self.stdout.write(
                self.style.WARNING(f"\n  Could not generate compose.ollama.yaml: {exc}\n")
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f"\n  Generated {output_path.name} (model: {model})\n")
        )
        self.stdout.write(
            "  Run with Docker:\n"
            "    docker compose -f compose.yaml -f compose.ollama.yaml up\n"
            "    # or: ./scripts/launch.sh\n"
        )

    def _generate_llama_cpp_compose(self, model: str) -> None:
        """Generate compose.llama-cpp.yaml from the template with the chosen model."""
        base_dir = self._get_project_root()
        template_path = base_dir / "docker" / "compose.llama-cpp.yaml.template"
        output_path = base_dir / "compose.llama-cpp.yaml"

        if not template_path.exists():
            self.stdout.write(self.style.WARNING(f"\n  Template not found: {template_path}\n"))
            return

        try:
            content = template_path.read_text(encoding="utf-8")
            content = content.replace("{{MODEL}}", model)
            output_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            self.stdout.write(
                self.style.WARNING(f"\n  Could not generate compose.llama-cpp.yaml: {exc}\n")
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f"\n  Generated {output_path.name} (model: {model})\n")
        )
        self.stdout.write(
            "  Run with Docker:\n"
            "    docker compose -f compose.yaml -f compose.llama-cpp.yaml up\n"
            "    # or: ./scripts/launch.sh\n"
        )

    def _generate_vllm_compose(self, model: str) -> None:
        """Generate compose.vllm.yaml from the template with the chosen model."""
        base_dir = self._get_project_root()
        template_path = base_dir / "docker" / "compose.vllm.yaml.template"
        output_path = base_dir / "compose.vllm.yaml"

        if not template_path.exists():
            self.stdout.write(self.style.WARNING(f"\n  Template not found: {template_path}\n"))
            return

        try:
            content = template_path.read_text(encoding="utf-8")
            content = content.replace("{{MODEL}}", model)
            output_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            self.stdout.write(
                self.style.WARNING(f"\n  Could not generate compose.vllm.yaml: {exc}\n")
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f"\n  Generated {output_path.name} (model: {model})\n")
        )
        self.stdout.write(
            "  Run with Docker:\n"
            "    docker compose -f compose.yaml -f compose.vllm.yaml up\n"
            "    # or: ./scripts/launch.sh\n"
        )

    def _configure_llama_cpp(self, llm_config: dict[str, object]) -> dict[str, object]:
        """Configure llama.cpp server as an OpenAI-compatible backend."""
        if not self._docker_mode and not shutil.which("llama-server"):
            self.stdout.write(
                self.style.WARNING(
                    "\n  llama-server not found on PATH.\n"
                    "  Install: brew install llama.cpp (macOS)"
                    " or sudo apt-get install llama.cpp (Ubuntu)\n"
                    "  Or via Docker: docker compose --profile inference up llama-cpp\n"
                )
            )

        # llama.cpp exposes an OpenAI-compatible API, so use the openai provider.
        llm_config["provider"] = "openai"

        recommended_gguf, reason = recommend_gguf_model()
        self.stdout.write(f"\n  Hardware detection: {reason}\n")
        self.stdout.write(f"  Recommended GGUF model: {recommended_gguf}\n")

        if self._docker_mode:
            base_url = "http://llama-cpp:8080/v1"
            self.stdout.write(f"  Using Docker service URL: {base_url}\n")
        else:
            base_url = self._ask("  llama.cpp server URL: ", default="http://localhost:8080/v1")
        llm_config["base_url"] = base_url

        if self._docker_mode:
            model = self._ask("  GGUF model filename: ", default=recommended_gguf)
        else:
            model = self._discover_and_choose_model(
                provider="openai",
                default_model=recommended_gguf,
                base_url=base_url,
            )
        llm_config["model"] = model

        if self._docker_mode:
            self._generate_llama_cpp_compose(model)
            self.stdout.write(
                "  Place the GGUF file in the llama-models volume before starting:\n"
                "    docker run --rm -v llama-models:/models -v $(pwd):/src alpine "
                "cp /src/" + model + " /models/\n"
            )
        else:
            self.stdout.write(
                "\n  To start the server, run:\n"
                f"    llama-server -m <path-to-{model}> --port 8080\n"
                "  Or via Docker:\n"
                "    docker compose --profile inference up llama-cpp\n"
            )

        return llm_config

    def _configure_vllm(self, llm_config: dict[str, object]) -> dict[str, object]:
        """Configure vLLM server as an OpenAI-compatible backend."""
        if not self._docker_mode and not shutil.which("vllm"):
            self.stdout.write(
                self.style.WARNING(
                    "\n  vllm not found on PATH.\n"
                    "  Install: pip install vllm\n"
                    "  Or via Docker: docker compose --profile inference up vllm\n"
                )
            )

        # vLLM exposes an OpenAI-compatible API, so use the openai provider.
        llm_config["provider"] = "openai"

        recommended_hf, reason = recommend_hf_model()
        self.stdout.write(f"\n  Hardware detection: {reason}\n")
        self.stdout.write(f"  Recommended HuggingFace model: {recommended_hf}\n")

        if self._docker_mode:
            base_url = "http://vllm:8000/v1"
            self.stdout.write(f"  Using Docker service URL: {base_url}\n")
        else:
            base_url = self._ask("  vLLM server URL: ", default="http://localhost:8081/v1")
        llm_config["base_url"] = base_url

        if self._docker_mode:
            model = self._ask("  HuggingFace model name: ", default=recommended_hf)
        else:
            model = self._discover_and_choose_model(
                provider="openai",
                default_model=recommended_hf,
                base_url=base_url,
            )
        llm_config["model"] = model

        if self._docker_mode:
            self._generate_vllm_compose(model)
            self.stdout.write("  Model will be auto-downloaded by vLLM on first start.\n")
        else:
            self.stdout.write(
                "\n  To start the server, run:\n"
                f"    vllm serve {model}\n"
                "  Or via Docker:\n"
                "    docker compose --profile inference up vllm\n"
            )

        return llm_config

    def _configure_custom_endpoint(
        self,
        llm_backends: list[dict[str, object]],
        env_vars_needed: list[str],
    ) -> list[dict[str, object]]:
        """Prompt for a custom OpenAI-compatible endpoint when nothing else was configured."""
        self.stdout.write("\nNo LLM backend configured.\n")
        self.stdout.write("You can specify a custom OpenAI-compatible endpoint.\n\n")

        endpoint = self._ask(
            "  Endpoint URL or env var name (Enter to skip): ",
        )
        if not endpoint:
            return llm_backends

        # Resolve: if it looks like a URL use directly, else read from env.
        if endpoint.startswith(("http://", "https://")):
            resolved_url = endpoint
        else:
            resolved_url = os.environ.get(endpoint, "")
            if resolved_url:
                self.stdout.write(f"  Resolved {endpoint} = {resolved_url[:30]}...\n")
            else:
                self.stdout.write(
                    self.style.WARNING(f"  {endpoint} not found in environment, using as-is.\n")
                )
                resolved_url = endpoint

        token_input = self._ask(
            "  API token or env var name (Enter if none needed): ",
        )

        api_key_env = ""
        if token_input:
            # If it looks like a raw token (long, starts with key prefix), suggest
            # setting an env var; otherwise treat it as an env var name.
            key_prefixes = ("sk-", "key-", "pk-", "rk-", "gsk_", "xai-", "pplx-")
            if token_input.startswith(key_prefixes) or len(token_input) > 40:
                self.stdout.write(
                    "\n  Tip: store your token in an env var instead of config:\n"
                    "    export FRANK_LLM_API_KEY=<your-token>\n"
                )
                api_key_env = "FRANK_LLM_API_KEY"
                os.environ["FRANK_LLM_API_KEY"] = token_input
            else:
                api_key_env = token_input
                env_vars_needed.append(token_input)

        model = self._ask("  Model name: ", default="")

        custom_config: dict[str, object] = {
            "provider": "openai",
            "base_url": resolved_url,
            "model": model,
        }
        if api_key_env:
            custom_config["api_key_env"] = api_key_env

        llm_backends.append(custom_config)
        return llm_backends

    def _configure_detected_backend(self, entry: DynamicMenuEntry) -> dict[str, object]:
        """Configure a Tier 2/3 detected credential as an OpenAI-compatible backend."""
        config: dict[str, object] = {"provider": "openai"}
        if entry.api_key_env:
            config["api_key_env"] = entry.api_key_env

        if entry.base_url_env:
            endpoint_val = os.environ.get(entry.base_url_env, "")
            if endpoint_val:
                config["base_url"] = endpoint_val
                self.stdout.write(f"  Using endpoint from {entry.base_url_env}\n")
            else:
                base_url = self._ask(
                    "  Base URL (e.g. https://api.groq.com/openai/v1): ", default=""
                )
                if base_url:
                    config["base_url"] = base_url
        else:
            base_url = self._ask("  Base URL (e.g. https://api.groq.com/openai/v1): ", default="")
            if base_url:
                config["base_url"] = base_url

        model = self._discover_and_choose_model(
            provider="openai",
            default_model="",
            api_key_env=entry.api_key_env,
            base_url=str(config.get("base_url", "")),
        )
        config["model"] = model

        temp = self._ask("  Temperature (0.0-2.0): ", default="0.3")
        try:
            config["temperature"] = float(temp)
        except ValueError:
            config["temperature"] = 0.3

        return config

    def _configure_initial_projects(self, operator_path: Path) -> None:
        """Prompt for initial GitHub repositories to monitor."""
        self.stdout.write("\n--- Projects to Monitor ---\n")
        self.stdout.write(
            "  Add GitHub repositories you want to review.\n"
            "  You can add more later with: python manage.py add_project --repo owner/repo\n\n"
        )
        repos_raw = self._ask(
            "  Repos (comma-separated, e.g. 'apache/spark,myorg/myrepo'): ",
            default="",
        )
        if not repos_raw:
            return

        projects_dir = operator_path.parent / "projects"
        projects_dir.mkdir(parents=True, exist_ok=True)

        for repo_raw in repos_raw.split(","):
            repo = repo_raw.strip()
            if not repo:
                continue
            parts = repo.split("/", 1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                self.stdout.write(
                    self.style.WARNING(f"  Skipping '{repo}' — expected owner/repo format\n")
                )
                continue
            owner, repo_name = parts

            governance = self._ask(
                f"  Governance for {repo} (standard/asf/personal): ",
                default="standard",
            )
            if governance not in ("standard", "asf", "personal"):
                governance = "standard"

            filename = f"{owner}-{repo_name}.yaml"
            filepath = projects_dir / filename
            yaml_content = (
                f'owner: "{owner}"\n'
                f'repo: "{repo_name}"\n'
                f'review_context: "general open-source"\n'
                f'governance: "{governance}"\n'
                f'tone: "direct"\n'
                f"watched_paths: []\n"
                f"ignore_paths: []\n"
                f"frequent_contributors: []\n"
                f"enabled: true\n"
            )
            filepath.write_text(yaml_content, encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"  Created {filepath}\n"))

    def _configure_coderabbit(self) -> dict[str, object] | None:
        """Optionally configure CodeRabbit CLI integration."""
        self.stdout.write("\n--- CodeRabbit CLI ---\n")
        enable = self._ask("Enable CodeRabbit CLI integration? (y/N): ", default="n")

        if enable.lower() not in ("y", "yes"):
            return None

        cr_config: dict[str, object] = {"enabled": True}

        # Check if coderabbit is on PATH.
        if shutil.which("coderabbit"):
            self.stdout.write(self.style.SUCCESS("  Found 'coderabbit' on PATH.\n"))
            cr_config["cli_path"] = "coderabbit"
        else:
            self.stdout.write(
                self.style.WARNING(
                    "  'coderabbit' not found on PATH.\n  Install: npm install -g coderabbitai\n"
                )
            )
            cli_path = self._ask("Path to coderabbit CLI: ", default="coderabbit")
            cr_config["cli_path"] = cli_path

        return cr_config
