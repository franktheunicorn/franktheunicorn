"""Ollama local-model backend for review generation."""

from __future__ import annotations

import platform
import shutil
import subprocess

import psutil

from franktheunicorn.review.backends.base import BaseLLMBackend

_DEFAULT_MODEL = "qwen2.5-coder:14b"

# (min_gb, model) — checked in order, first match wins.
_VRAM_TIERS: list[tuple[float, str]] = [
    (24, "qwen2.5-coder:32b"),
    (12, "qwen2.5-coder:14b"),
    (6, "qwen2.5-coder:7b"),
]

_RAM_TIERS: list[tuple[float, str]] = [
    (32, "qwen2.5-coder:32b"),
    (16, "qwen2.5-coder:14b"),
    (8, "qwen2.5-coder:7b"),
]


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
            total_mib = sum(
                float(line.strip()) for line in result.stdout.strip().split("\n") if line.strip()
            )
            return total_mib / 1024.0
    except Exception:
        pass
    return 0.0


def _get_total_ram_gb() -> float:
    return float(psutil.virtual_memory().total) / (1024**3)


def _pick_model(tiers: list[tuple[float, str]], available_gb: float) -> str | None:
    """Return the largest model that fits in ``available_gb``, or None."""
    for min_gb, model in tiers:
        if available_gb >= min_gb:
            return model
    return None


def recommend_local_model() -> tuple[str, str]:
    """Recommend an Ollama model based on available hardware.

    Returns ``(model_name, reason)`` tuple.
    """
    is_apple = platform.system() == "Darwin" and platform.machine() == "arm64"

    if is_apple:
        ram_gb = _get_total_ram_gb()
        model = _pick_model(_RAM_TIERS, ram_gb) or "qwen2.5-coder:7b"
        return (model, f"Apple Silicon with {ram_gb:.0f}GB unified memory")

    nvidia_vram = _get_nvidia_vram_gb()
    if nvidia_vram >= 6:
        model = _pick_model(_VRAM_TIERS, nvidia_vram) or "qwen2.5-coder:7b"
        return (model, f"{nvidia_vram:.0f}GB VRAM available")

    ram_gb = _get_total_ram_gb()
    model = _pick_model([(16, "qwen2.5-coder:7b")], ram_gb) or "qwen2.5-coder:3b"
    return (model, f"No GPU detected, {ram_gb:.0f}GB RAM (CPU inference)")


class OllamaBackend(BaseLLMBackend):
    """Ollama Python SDK backend for local model inference."""

    _sdk_module = "ollama"
    _default_key_env = ""
    _default_model = _DEFAULT_MODEL

    def _call_api(self, system_prompt: str, user_message: str, api_key: str) -> str:
        import ollama

        client = ollama.Client(host=self._config.base_url or None)
        response = client.chat(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            format="json",
            options={"temperature": self._config.temperature},
        )
        return response.message.content or ""
