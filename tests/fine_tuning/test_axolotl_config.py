"""Tests for Axolotl config generation (v2 — §10.3)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from franktheunicorn.fine_tuning.axolotl_config import (
    DEFAULT_EPOCHS,
    DEFAULT_GRAD_ACCUM,
    DEFAULT_LORA_ALPHA,
    DEFAULT_LORA_R,
    DEFAULT_LR,
    DEFAULT_MICRO_BATCH,
    DEFAULT_SEQUENCE_LEN,
    generate_axolotl_config,
)


class TestGenerateAxolotlConfig:
    def _setup_dataset(self, tmp_path: Path, train_count: int = 200) -> Path:
        """Create a minimal dataset directory with train.jsonl and metadata."""
        dataset_dir = tmp_path / "training-data" / "test-project"
        dataset_dir.mkdir(parents=True)

        train_path = dataset_dir / "train.jsonl"
        train_path.write_text(
            '{"instruction": "test", "input": "test", "output": "test"}\n' * train_count
        )

        metadata = {
            "project": "test/project",
            "train_count": train_count,
            "eval_count": train_count // 5,
        }
        (dataset_dir / "metadata.json").write_text(json.dumps(metadata))
        return dataset_dir

    def test_generates_valid_yaml(self, tmp_path: Path) -> None:
        dataset_dir = self._setup_dataset(tmp_path)
        result = generate_axolotl_config("test/project", dataset_dir)

        assert result.error == ""
        assert result.config_path.exists()

        config = yaml.safe_load(result.config_path.read_text())
        assert config["base_model"] == "Qwen/Qwen2.5-Coder-7B-Instruct"

    def test_default_qlora_params(self, tmp_path: Path) -> None:
        dataset_dir = self._setup_dataset(tmp_path)
        result = generate_axolotl_config("test/project", dataset_dir)

        config = result.config
        assert config["adapter"] == "qlora"
        assert config["load_in_4bit"] is True
        assert config["load_in_8bit"] is False
        assert config["lora_r"] == DEFAULT_LORA_R
        assert config["lora_alpha"] == DEFAULT_LORA_ALPHA
        assert config["sequence_len"] == DEFAULT_SEQUENCE_LEN
        assert config["sample_packing"] is True
        assert config["micro_batch_size"] == DEFAULT_MICRO_BATCH
        assert config["gradient_accumulation_steps"] == DEFAULT_GRAD_ACCUM
        assert config["num_epochs"] == DEFAULT_EPOCHS
        assert config["learning_rate"] == DEFAULT_LR
        assert config["lr_scheduler"] == "cosine"

    def test_custom_base_model(self, tmp_path: Path) -> None:
        dataset_dir = self._setup_dataset(tmp_path)
        result = generate_axolotl_config(
            "test/project",
            dataset_dir,
            base_model="mistralai/Mistral-7B-v0.3",
        )

        assert result.config["base_model"] == "mistralai/Mistral-7B-v0.3"
        assert result.config["model_type"] == "MistralForCausalLM"

    def test_version_numbering(self, tmp_path: Path) -> None:
        dataset_dir = self._setup_dataset(tmp_path)
        models_dir = tmp_path / "models"

        # First version.
        result1 = generate_axolotl_config("test/project", dataset_dir, models_dir=models_dir)
        assert result1.version == 1
        assert "v1" in str(result1.output_dir)

        # Create the v1 directory to simulate completed training.
        result1.output_dir.mkdir(parents=True)

        # Second version.
        result2 = generate_axolotl_config("test/project", dataset_dir, models_dir=models_dir)
        assert result2.version == 2
        assert "v2" in str(result2.output_dir)

    def test_qlora_8bit(self, tmp_path: Path) -> None:
        dataset_dir = self._setup_dataset(tmp_path)
        result = generate_axolotl_config("test/project", dataset_dir, quantization="qlora-8bit")

        assert result.config["load_in_4bit"] is False
        assert result.config["load_in_8bit"] is True
        assert result.config["adapter"] == "qlora"

    def test_lora_no_quantization(self, tmp_path: Path) -> None:
        dataset_dir = self._setup_dataset(tmp_path)
        result = generate_axolotl_config("test/project", dataset_dir, quantization="lora")

        assert result.config["load_in_4bit"] is False
        assert result.config["load_in_8bit"] is False
        assert result.config["adapter"] == "lora"

    def test_missing_train_data(self, tmp_path: Path) -> None:
        dataset_dir = tmp_path / "empty"
        dataset_dir.mkdir()

        result = generate_axolotl_config("test/project", dataset_dir)
        assert "not found" in result.error

    def test_dataset_path_in_config(self, tmp_path: Path) -> None:
        dataset_dir = self._setup_dataset(tmp_path)
        result = generate_axolotl_config("test/project", dataset_dir)

        datasets = result.config["datasets"]
        assert len(datasets) == 1
        assert datasets[0]["type"] == "alpaca"
        assert "train.jsonl" in datasets[0]["path"]

    def test_metadata_included(self, tmp_path: Path) -> None:
        dataset_dir = self._setup_dataset(tmp_path, train_count=300)
        result = generate_axolotl_config("test/project", dataset_dir)

        assert "_metadata" in result.config
        assert result.config["_metadata"]["project"] == "test/project"
        assert result.config["_metadata"]["train_examples"] == 300

    def test_custom_training_params(self, tmp_path: Path) -> None:
        dataset_dir = self._setup_dataset(tmp_path)
        result = generate_axolotl_config(
            "test/project",
            dataset_dir,
            sequence_len=2048,
            micro_batch_size=4,
            num_epochs=5,
            learning_rate=1e-4,
        )

        assert result.config["sequence_len"] == 2048
        assert result.config["micro_batch_size"] == 4
        assert result.config["num_epochs"] == 5
        assert result.config["learning_rate"] == 1e-4

    def test_output_dir_uses_project_name(self, tmp_path: Path) -> None:
        dataset_dir = self._setup_dataset(tmp_path)
        models_dir = tmp_path / "models"
        result = generate_axolotl_config("apache/spark", dataset_dir, models_dir=models_dir)

        assert "apache-spark" in str(result.output_dir)
