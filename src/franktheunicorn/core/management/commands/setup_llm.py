"""Interactive setup wizard for LLM backend configuration.

Usage::

    python manage.py setup_llm
    python manage.py setup_llm --output ~/.review-agent/operator.yaml
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml
from django.core.management.base import BaseCommand

from franktheunicorn.review.backends.ollama_backend import recommend_local_model

# (provider_id, label, default_model, api_key_env)
_PROVIDERS: dict[str, tuple[str, str, str, str]] = {
    "1": ("claude", "Anthropic Claude", "claude-sonnet-4-20250514", "ANTHROPIC_API_KEY"),
    "2": ("openai", "OpenAI", "gpt-4o", "OPENAI_API_KEY"),
    "3": ("gemini", "Google Gemini", "gemini-2.5-flash", "GOOGLE_API_KEY"),
    "4": ("ollama", "Local model (Ollama)", "qwen2.5-coder:14b", ""),
}


class Command(BaseCommand):
    help = "Interactive setup wizard for LLM backend and CodeRabbit configuration."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument(
            "--output",
            default="",
            help="Path to write operator.yaml (default: ~/.review-agent/operator.yaml)",
        )

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        self.stdout.write(self.style.SUCCESS("\n=== franktheunicorn LLM Setup Wizard ===\n"))

        config: dict[str, object] = {}

        # --- GitHub username ---
        config["github_username"] = self._ask(
            "GitHub username (for scoring — leave blank to skip): ",
            default="",
        )

        # --- Review style ---
        config["review_style"] = self._ask(
            "Review style/tone (e.g. 'direct but kind', 'thorough and formal'): ",
            default="direct but kind",
        )

        # --- LLM providers (multiple) ---
        self.stdout.write("\nSelect LLM providers for code review (you can enable multiple):\n")
        for key, (_, label, _, _) in _PROVIDERS.items():
            self.stdout.write(f"  {key}. {label}\n")
        self.stdout.write("  5. Skip — use stub/demo mode\n")

        choices_raw = self._ask(
            "Enter choices (comma-separated, e.g. '1,4' for Claude + Ollama): ",
            default="5",
        )
        chosen_keys = [c.strip() for c in choices_raw.split(",")]

        llm_backends: list[dict[str, object]] = []
        env_vars_needed: list[str] = []

        for key in chosen_keys:
            if key == "5":
                continue
            if key not in _PROVIDERS:
                self.stdout.write(self.style.WARNING(f"  Skipping unknown choice '{key}'\n"))
                continue
            provider, provider_label, _default_model, api_key_env = _PROVIDERS[key]
            self.stdout.write(f"\n--- Configuring {provider_label} ---\n")

            llm_config: dict[str, object] = {"provider": provider}

            if api_key_env:
                llm_config = self._configure_cloud_provider(provider, llm_config)
                env_vars_needed.append(api_key_env)
            elif provider == "ollama":
                llm_config = self._configure_ollama(llm_config)

            llm_backends.append(llm_config)

        if llm_backends:
            config["llm_backends"] = llm_backends

        # --- CodeRabbit ---
        cr_config = self._configure_coderabbit()
        if cr_config:
            config["coderabbit"] = cr_config

        # --- Write config ---
        output_path = options.get("output") or ""
        if not output_path:
            output_path = str(Path.home() / ".review-agent" / "operator.yaml")
        output_path_obj = Path(output_path)

        self.stdout.write(f"\nConfig will be written to: {output_path_obj}\n")

        output_path_obj.parent.mkdir(parents=True, exist_ok=True)
        with output_path_obj.open("w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        self.stdout.write(self.style.SUCCESS(f"\nConfig saved to {output_path_obj}"))
        self.stdout.write("\nTo use this config, set:\n")
        self.stdout.write(f"  export FRANK_OPERATOR_CONFIG={output_path_obj}\n")

        for env_var in env_vars_needed:
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
            self.stdout.write(
                f"\n  Set the {env_var} environment variable with your API key.\n"
                f"  Example: export {env_var}=sk-...\n"
            )

        llm_config["api_key_env"] = env_var

        model = self._ask("Model name: ", default=default_model)
        llm_config["model"] = model

        temp = self._ask("Temperature (0.0-2.0): ", default="0.3")
        try:
            llm_config["temperature"] = float(temp)
        except ValueError:
            llm_config["temperature"] = 0.3

        return llm_config

    def _configure_ollama(self, llm_config: dict[str, object]) -> dict[str, object]:
        """Configure local Ollama backend with auto-detected model recommendation."""
        # Check if ollama is installed.
        if not shutil.which("ollama"):
            self.stdout.write(
                self.style.WARNING(
                    "\n  Ollama not found on PATH.\n  Install from: https://ollama.com/download\n"
                )
            )

        recommended_model, reason = recommend_local_model()
        self.stdout.write(f"\n  Hardware detection: {reason}\n")
        self.stdout.write(f"  Recommended model: {recommended_model}\n")

        model = self._ask("Model name: ", default=recommended_model)
        llm_config["model"] = model

        base_url = self._ask("Ollama server URL: ", default="http://localhost:11434")
        llm_config["base_url"] = base_url

        self.stdout.write(f"\n  To download the model, run:\n    ollama pull {model}\n")

        return llm_config

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
