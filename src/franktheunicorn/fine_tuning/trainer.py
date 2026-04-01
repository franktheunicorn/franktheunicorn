"""Fine-tuning training execution (v2 — §10.4).

Orchestrates the full training flow: validation, Axolotl invocation,
model saving, and evaluation gating.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Minimum dataset size for training.
MIN_TRAINING_EXAMPLES = 200

# Minimum approval rate in dataset.
MIN_APPROVAL_RATE = 0.7


@dataclass
class ValidationResult:
    """Result of dataset validation."""

    valid: bool = True
    total_examples: int = 0
    action_distribution: dict[str, int] = field(default_factory=dict)
    approval_rate: float = 0.0
    errors: list[str] = field(default_factory=list)


@dataclass
class TrainingResult:
    """Result of a fine-tuning training run."""

    success: bool = False
    model_dir: Path | None = None
    version: int = 0
    eval_passed: bool = False
    eval_metrics: dict[str, float] = field(default_factory=dict)
    error: str = ""
    training_log: str = ""


def validate_dataset(
    dataset_dir: Path,
    *,
    min_examples: int = MIN_TRAINING_EXAMPLES,
    min_approval_rate: float = MIN_APPROVAL_RATE,
) -> ValidationResult:
    """Validate that a dataset is suitable for training.

    Checks:
    - Minimum number of examples
    - Action distribution (not all rejects, not all accepts)
    - Minimum approval rate
    """
    result = ValidationResult()

    metadata_path = dataset_dir / "metadata.json"
    if not metadata_path.exists():
        result.valid = False
        result.errors.append(f"metadata.json not found in {dataset_dir}")
        return result

    metadata = json.loads(metadata_path.read_text())
    total = metadata.get("total_examples", 0)
    result.total_examples = total

    if total < min_examples:
        result.valid = False
        result.errors.append(f"Not enough examples: {total} < {min_examples}")

    # Check structure distribution.
    structure_counts = metadata.get("structure_counts", {})
    result.action_distribution = structure_counts

    # Compute approval rate from structure counts.
    # Rejected (pure-critique) vs everything else.
    critique_count = structure_counts.get("pure-critique", 0)
    positive_count = total - critique_count
    result.approval_rate = positive_count / total if total > 0 else 0.0

    if result.approval_rate < min_approval_rate:
        result.valid = False
        result.errors.append(
            f"Approval rate too low: {result.approval_rate:.2f} < {min_approval_rate}"
        )

    # Check for degenerate distributions.
    if total > 0 and len(structure_counts) < 2:
        result.errors.append("Warning: only one structure type in dataset — model may overfit")

    return result


def _run_axolotl_local(config_path: Path) -> tuple[bool, str]:
    """Run Axolotl training locally via subprocess."""
    cmd = ["python", "-m", "axolotl.cli.train", str(config_path)]
    logger.info("Running Axolotl locally: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600 * 12,  # 12 hour timeout
        )
        if result.returncode == 0:
            return True, result.stdout
        return False, f"Axolotl failed (exit {result.returncode}):\n{result.stderr}"
    except FileNotFoundError:
        return False, "Axolotl not installed. Install with: pip install axolotl"
    except subprocess.TimeoutExpired:
        return False, "Training timed out after 12 hours"


def _run_axolotl_docker(config_path: Path, dataset_dir: Path) -> tuple[bool, str]:
    """Run Axolotl training via Docker."""
    if not shutil.which("docker"):
        return False, "Docker not found on PATH"

    cmd = [
        "docker",
        "run",
        "--gpus",
        "all",
        "-v",
        f"{dataset_dir}:/workspace/data",
        "-v",
        f"{config_path}:/workspace/config.yaml",
        "winglian/axolotl:latest",
        "accelerate",
        "launch",
        "-m",
        "axolotl.cli.train",
        "/workspace/config.yaml",
    ]
    logger.info("Running Axolotl via Docker: %s", " ".join(cmd[:6]))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600 * 12,
        )
        if result.returncode == 0:
            return True, result.stdout
        return False, f"Docker Axolotl failed (exit {result.returncode}):\n{result.stderr}"
    except subprocess.TimeoutExpired:
        return False, "Docker training timed out after 12 hours"


def _save_training_metadata(
    output_dir: Path,
    config: dict[str, Any],
    training_result: TrainingResult,
) -> None:
    """Save training metadata for reproducibility."""
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "version": training_result.version,
        "base_model": config.get("base_model", ""),
        "adapter": config.get("adapter", ""),
        "success": training_result.success,
        "eval_passed": training_result.eval_passed,
        "eval_metrics": training_result.eval_metrics,
    }
    (output_dir / "training_metadata.json").write_text(json.dumps(metadata, indent=2))


def run_fine_tune(
    project_name: str,
    dataset_dir: Path,
    axolotl_config_path: Path,
    output_dir: Path,
    version: int,
    *,
    use_docker: bool = False,
    force: bool = False,
    eval_only: bool = False,
) -> TrainingResult:
    """Run the full fine-tuning pipeline.

    Steps:
    1. Validate dataset
    2. Run Axolotl training (local or Docker)
    3. Save adapter + metadata

    Evaluation is handled separately by the evaluator module.
    """
    result = TrainingResult(version=version)

    # Step 1: Validate dataset.
    if not eval_only:
        validation = validate_dataset(dataset_dir)
        if not validation.valid and not force:
            result.error = "; ".join(validation.errors)
            return result

    # Step 2: Run training.
    if not eval_only:
        if use_docker:
            success, log = _run_axolotl_docker(axolotl_config_path, dataset_dir)
        else:
            success, log = _run_axolotl_local(axolotl_config_path)

        result.training_log = log
        if not success:
            result.error = log
            return result

        result.success = True

    # Step 3: Save metadata.
    config: dict[str, Any] = {}
    if axolotl_config_path.exists():
        import yaml

        config = yaml.safe_load(axolotl_config_path.read_text()) or {}

    result.model_dir = output_dir
    _save_training_metadata(output_dir, config, result)

    logger.info(
        "Fine-tuning %s for %s (v%d): %s",
        "completed" if result.success else "eval-only",
        project_name,
        version,
        "success" if result.success else "pending eval",
    )

    return result
