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

from franktheunicorn.config.credential_detection import (
    DetectedCredential,
    detect_llm_credentials,
    format_detections,
    get_openai_compatible_detections,
    suggest_provider_choices,
)
from franktheunicorn.config.model_discovery import (
    discover_models,
    format_model_menu,
)
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

        # --- Detect credentials from environment ---
        detections = detect_llm_credentials()
        if detections:
            self.stdout.write("\n")
            self.stdout.write(format_detections(detections))

        default_choice = suggest_provider_choices(detections)

        # --- LLM providers (multiple) ---
        self.stdout.write("\nSelect LLM providers for code review (you can enable multiple):\n")
        for key, (_, label, _, _) in _PROVIDERS.items():
            self.stdout.write(f"  {key}. {label}\n")
        self.stdout.write("  5. Skip — use stub/demo mode\n")

        choices_raw = self._ask(
            "Enter choices (comma-separated, e.g. '1,4' for Claude + Ollama): ",
            default=default_choice,
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

        # --- Offer OpenAI-compatible backends for Tier 2/3 detections ---
        compat_detections = get_openai_compatible_detections(detections)
        if compat_detections:
            llm_backends = self._offer_openai_compatible(
                compat_detections, llm_backends, env_vars_needed
            )

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

    def _discover_and_choose_model(
        self,
        provider: str,
        default_model: str,
        api_key_env: str = "",
        base_url: str = "",
    ) -> str:
        """Try to list models from the API and let the user pick one."""
        self.stdout.write("\n  Discovering available models...\n")
        models = discover_models(
            provider=provider,
            api_key_env=api_key_env,
            base_url=base_url,
        )
        if not models:
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
            self.stdout.write(
                f"\n  Set the {env_var} environment variable with your API key.\n"
                f"  Example: export {env_var}=sk-...\n"
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

        base_url = self._ask("  Ollama server URL: ", default="http://localhost:11434")
        llm_config["base_url"] = base_url

        # Try to discover available models from the Ollama server.
        model = self._discover_and_choose_model(
            provider="ollama",
            default_model=recommended_model,
            base_url=base_url,
        )
        llm_config["model"] = model

        self.stdout.write(f"\n  To download the model, run:\n    ollama pull {model}\n")

        return llm_config

    def _offer_openai_compatible(
        self,
        compat_detections: list[DetectedCredential],
        llm_backends: list[dict[str, object]],
        env_vars_needed: list[str],
    ) -> list[dict[str, object]]:
        """Offer to configure Tier 2/3 detections as OpenAI-compatible backends."""
        providers_already_configured = {str(b.get("provider", "")) for b in llm_backends}

        # Group by provider to avoid duplicate offers.
        seen_providers: set[str] = set()
        candidates: list[tuple[str, str, str]] = []  # (provider, env_var, endpoint)
        for d in compat_detections:
            if d.provider in seen_providers:
                continue
            seen_providers.add(d.provider)
            endpoint = ""
            if d.credential_type == "endpoint":
                endpoint = d.env_var
            elif d.paired_with:
                endpoint = d.paired_with
            candidates.append((d.provider or "unknown", d.env_var, endpoint))

        if not candidates:
            return llm_backends

        self.stdout.write("\n--- Additional LLM Providers Detected ---\n")
        for prov, env_var, endpoint in candidates:
            extra = f" (endpoint: {endpoint})" if endpoint else ""
            self.stdout.write(f"  {prov}: {env_var}{extra}\n")

        enable = self._ask(
            "\nConfigure as OpenAI-compatible backend(s)? (y/N): ",
            default="n",
        )
        if enable.lower() not in ("y", "yes"):
            return llm_backends

        for prov, env_var, endpoint in candidates:
            if "openai" in providers_already_configured:
                # Use a distinct entry with base_url.
                pass
            self.stdout.write(f"\n--- Configuring {prov} (OpenAI-compatible) ---\n")
            compat_config: dict[str, object] = {"provider": "openai"}
            compat_config["api_key_env"] = env_var

            if endpoint:
                endpoint_val = os.environ.get(endpoint, "")
                if endpoint_val:
                    compat_config["base_url"] = endpoint_val
                    self.stdout.write(f"  Using endpoint from {endpoint}\n")
            else:
                base_url = self._ask(
                    "  Base URL (e.g. https://api.groq.com/openai/v1): ", default=""
                )
                if base_url:
                    compat_config["base_url"] = base_url

            # Try model discovery on the compatible endpoint.
            model = self._discover_and_choose_model(
                provider="openai",
                default_model="",
                api_key_env=env_var,
                base_url=str(compat_config.get("base_url", "")),
            )
            compat_config["model"] = model

            temp = self._ask("  Temperature (0.0-2.0): ", default="0.3")
            try:
                compat_config["temperature"] = float(temp)
            except ValueError:
                compat_config["temperature"] = 0.3

            llm_backends.append(compat_config)
            env_vars_needed.append(env_var)

        return llm_backends

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
