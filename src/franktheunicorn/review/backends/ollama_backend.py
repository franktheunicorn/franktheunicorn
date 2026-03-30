"""Ollama local-model backend for review generation."""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from typing import TYPE_CHECKING

from franktheunicorn.review.backends.base import PRContext, ReviewFinding, parse_llm_response
from franktheunicorn.review.prompt import build_system_prompt, build_user_message

if TYPE_CHECKING:
    from franktheunicorn.config.models import LLMBackendConfig

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "qwen2.5-coder:14b"


def _get_nvidia_vram_gb() -> float:
    """Query nvidia-smi for total VRAM in GB. Returns 0.0 if unavailable."""
    if not shutil.which("nvidia-smi"):
        return 0.0
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Sum across GPUs, nvidia-smi reports in MiB.
            total_mib = sum(
                float(line.strip()) for line in result.stdout.strip().split("\n") if line.strip()
            )
            return total_mib / 1024.0
    except Exception:
        pass
    return 0.0


def _is_apple_silicon() -> bool:
    """Check if running on Apple Silicon (arm64 macOS)."""
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def _get_total_ram_gb() -> float:
    """Get total system RAM in GB."""
    try:
        import psutil

        return float(psutil.virtual_memory().total) / (1024**3)
    except ImportError:
        # Fallback: read /proc/meminfo on Linux.
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return kb / (1024**2)
        except Exception:
            pass
    return 0.0


def recommend_local_model() -> tuple[str, str]:
    """Recommend an Ollama model based on available hardware.

    Returns ``(model_name, reason)`` tuple.
    """
    if _is_apple_silicon():
        ram_gb = _get_total_ram_gb()
        if ram_gb >= 32:
            return ("qwen2.5-coder:32b", f"Apple Silicon with {ram_gb:.0f}GB unified memory")
        if ram_gb >= 16:
            return ("qwen2.5-coder:14b", f"Apple Silicon with {ram_gb:.0f}GB unified memory")
        return ("qwen2.5-coder:7b", f"Apple Silicon with {ram_gb:.0f}GB unified memory")

    nvidia_vram = _get_nvidia_vram_gb()
    if nvidia_vram >= 24:
        return ("qwen2.5-coder:32b", f"{nvidia_vram:.0f}GB VRAM available")
    if nvidia_vram >= 12:
        return ("qwen2.5-coder:14b", f"{nvidia_vram:.0f}GB VRAM available")
    if nvidia_vram >= 6:
        return ("qwen2.5-coder:7b", f"{nvidia_vram:.0f}GB VRAM available")

    # CPU fallback — check RAM for quantised models.
    ram_gb = _get_total_ram_gb()
    if ram_gb >= 16:
        return ("qwen2.5-coder:7b", f"No GPU detected, {ram_gb:.0f}GB RAM (CPU inference)")
    return ("qwen2.5-coder:3b", f"No GPU detected, {ram_gb:.0f}GB RAM (small CPU model)")


class OllamaBackend:
    """Review backend using the Ollama Python SDK for local model inference."""

    def __init__(self, config: LLMBackendConfig) -> None:
        self._config = config
        self._model = config.model or _DEFAULT_MODEL

    def generate_findings(
        self,
        diff: str,
        pr_context: PRContext,
    ) -> list[ReviewFinding]:
        try:
            import ollama
        except ImportError:
            logger.error("ollama package not installed. Run: pip install 'franktheunicorn[llm]'")
            return []

        system_prompt = build_system_prompt(pr_context)
        user_message = build_user_message(diff, pr_context)

        if self._config.base_url:
            client = ollama.Client(host=self._config.base_url)
        else:
            client = ollama.Client()

        try:
            response = client.chat(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                format="json",
                options={"temperature": self._config.temperature},
            )
        except Exception:
            logger.exception("Ollama API call failed.")
            return []

        raw_text = response.message.content or ""
        return parse_llm_response(raw_text)
