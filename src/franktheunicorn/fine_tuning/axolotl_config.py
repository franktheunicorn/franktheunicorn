"""Axolotl config generation for QLoRA fine-tuning (v2 — §10.3).

Generates an axolotl_config.yaml from template parameters and dataset stats.
Default: QLoRA 4-bit on Qwen2.5-Coder-7B-Instruct, fits on a single 3090.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Default QLoRA training parameters (§10.3).
DEFAULT_LORA_R = 32
DEFAULT_LORA_ALPHA = 64
DEFAULT_LORA_DROPOUT = 0.05
DEFAULT_LORA_TARGET_MODULES = [
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
]

DEFAULT_SEQUENCE_LEN = 4096
DEFAULT_MICRO_BATCH = 2
DEFAULT_GRAD_ACCUM = 4
DEFAULT_EPOCHS = 3
DEFAULT_LR = 2e-4
DEFAULT_WARMUP_STEPS = 10

# Base model to model_type mapping.
_MODEL_TYPES: dict[str, str] = {
    "qwen": "AutoModelForCausalLM",
    "mistral": "MistralForCausalLM",
    "codellama": "LlamaForCausalLM",
    "llama": "LlamaForCausalLM",
    "deepseek": "AutoModelForCausalLM",
}


def _infer_model_type(base_model: str) -> str:
    """Infer the model type from the base model name."""
    lower = base_model.lower()
    for prefix, model_type in _MODEL_TYPES.items():
        if prefix in lower:
            return model_type
    return "AutoModelForCausalLM"


def _next_version(models_dir: Path, project_name: str) -> int:
    """Determine the next version number for a project's model."""
    safe_name = project_name.replace("/", "-")
    project_dir = models_dir / safe_name
    if not project_dir.exists():
        return 1
    existing = [
        int(d.name.lstrip("v"))
        for d in project_dir.iterdir()
        if d.is_dir() and d.name.startswith("v") and d.name[1:].isdigit()
    ]
    return max(existing, default=0) + 1


@dataclass
class AxolotlConfigResult:
    """Result of generating an Axolotl config."""

    config_path: Path | None
    output_dir: Path | None
    version: int
    config: dict[str, Any]
    error: str = ""


def generate_axolotl_config(
    project_name: str,
    dataset_dir: Path,
    base_model: str = "Qwen/Qwen2.5-Coder-7B-Instruct",
    models_dir: Path | None = None,
    *,
    quantization: str = "qlora-4bit",
    sequence_len: int = DEFAULT_SEQUENCE_LEN,
    micro_batch_size: int = DEFAULT_MICRO_BATCH,
    gradient_accumulation_steps: int = DEFAULT_GRAD_ACCUM,
    num_epochs: int = DEFAULT_EPOCHS,
    learning_rate: float = DEFAULT_LR,
) -> AxolotlConfigResult:
    """Generate an Axolotl YAML config for fine-tuning.

    Reads dataset metadata from ``dataset_dir/metadata.json`` to inform
    config parameters. Writes the config to ``dataset_dir/axolotl_config.yaml``.
    """
    # Validate dataset exists.
    train_path = dataset_dir / "train.jsonl"
    if not train_path.exists():
        return AxolotlConfigResult(
            config_path=None,
            output_dir=None,
            version=0,
            config={},
            error=f"Training data not found at {train_path}",
        )

    # Read metadata for dataset stats.
    metadata_path = dataset_dir / "metadata.json"
    metadata: dict[str, Any] = {}
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text())

    # Determine version and output dir.
    if models_dir is None:
        models_dir = dataset_dir.parent.parent / "models"
    version = _next_version(models_dir, project_name)
    safe_name = project_name.replace("/", "-")
    output_dir = models_dir / safe_name / f"v{version}"

    # Determine quantization flags.
    load_in_4bit = "4bit" in quantization
    load_in_8bit = "8bit" in quantization
    adapter = "qlora" if "qlora" in quantization else "lora"

    model_type = _infer_model_type(base_model)

    config: dict[str, Any] = {
        "base_model": base_model,
        "model_type": model_type,
        "load_in_8bit": load_in_8bit,
        "load_in_4bit": load_in_4bit,
        "adapter": adapter,
        "lora_r": DEFAULT_LORA_R,
        "lora_alpha": DEFAULT_LORA_ALPHA,
        "lora_dropout": DEFAULT_LORA_DROPOUT,
        "lora_target_modules": DEFAULT_LORA_TARGET_MODULES,
        "datasets": [
            {
                "path": str(train_path),
                "type": "alpaca",
            },
        ],
        "dataset_prepared_path": str(dataset_dir / "prepared"),
        "val_set_size": 0,
        "output_dir": str(output_dir),
        "sequence_len": sequence_len,
        "sample_packing": True,
        "micro_batch_size": micro_batch_size,
        "gradient_accumulation_steps": gradient_accumulation_steps,
        "num_epochs": num_epochs,
        "learning_rate": learning_rate,
        "lr_scheduler": "cosine",
        "warmup_steps": DEFAULT_WARMUP_STEPS,
        "bf16": "auto",
        "tf32": True,
        "logging_steps": 10,
        "save_strategy": "epoch",
        "eval_strategy": "epoch",
    }

    # Add dataset stats as comments in metadata.
    if metadata:
        config["_metadata"] = {
            "project": project_name,
            "version": version,
            "train_examples": metadata.get("train_count", 0),
            "eval_examples": metadata.get("eval_count", 0),
        }

    # Write config file.
    config_path = dataset_dir / "axolotl_config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    logger.info(
        "Generated Axolotl config at %s (version v%d, base: %s)",
        config_path,
        version,
        base_model,
    )

    return AxolotlConfigResult(
        config_path=config_path,
        output_dir=output_dir,
        version=version,
        config=config,
    )
