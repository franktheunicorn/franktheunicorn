"""Tests for fine-tuning training execution (v2 — §10.4)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from franktheunicorn.fine_tuning.trainer import (
    run_fine_tune,
    validate_dataset,
)


class TestValidateDataset:
    def _make_dataset(self, tmp_path: Path, total: int = 250, critique: int = 20) -> Path:
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()
        metadata = {
            "total_examples": total,
            "structure_counts": {
                "praise-suggestion": total - critique - 30,
                "direct-fix": 30,
                "pure-critique": critique,
            },
        }
        (dataset_dir / "metadata.json").write_text(json.dumps(metadata))
        return dataset_dir

    def test_valid_dataset(self, tmp_path: Path) -> None:
        dataset_dir = self._make_dataset(tmp_path)
        result = validate_dataset(dataset_dir)
        assert result.valid is True
        assert result.total_examples == 250
        assert result.approval_rate > 0.7

    def test_insufficient_examples(self, tmp_path: Path) -> None:
        dataset_dir = self._make_dataset(tmp_path, total=50)
        result = validate_dataset(dataset_dir)
        assert result.valid is False
        assert any("Not enough" in e for e in result.errors)

    def test_low_approval_rate(self, tmp_path: Path) -> None:
        dataset_dir = self._make_dataset(tmp_path, total=100, critique=80)
        result = validate_dataset(dataset_dir)
        assert result.valid is False
        assert any("Approval rate" in e for e in result.errors)

    def test_missing_metadata(self, tmp_path: Path) -> None:
        dataset_dir = tmp_path / "empty"
        dataset_dir.mkdir()
        result = validate_dataset(dataset_dir)
        assert result.valid is False
        assert any("metadata.json not found" in e for e in result.errors)


class TestRunFineTune:
    def _setup_env(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()

        metadata = {
            "total_examples": 250,
            "structure_counts": {"praise-suggestion": 200, "direct-fix": 50},
        }
        (dataset_dir / "metadata.json").write_text(json.dumps(metadata))
        (dataset_dir / "train.jsonl").write_text("{}\n" * 200)

        config_path = dataset_dir / "axolotl_config.yaml"
        config_path.write_text("base_model: test\nadapter: qlora\n")

        output_dir = tmp_path / "models" / "test" / "v1"
        return dataset_dir, config_path, output_dir

    @patch("franktheunicorn.fine_tuning.trainer._run_axolotl_local")
    def test_successful_training(self, mock_run: object, tmp_path: Path) -> None:
        mock_run.return_value = (True, "Training complete")  # type: ignore[attr-defined]
        dataset_dir, config_path, output_dir = self._setup_env(tmp_path)

        result = run_fine_tune("test/project", dataset_dir, config_path, output_dir, version=1)
        assert result.success is True
        assert result.version == 1
        assert result.model_dir == output_dir
        assert (output_dir / "training_metadata.json").exists()

    @patch("franktheunicorn.fine_tuning.trainer._run_axolotl_local")
    def test_training_failure(self, mock_run: object, tmp_path: Path) -> None:
        mock_run.return_value = (False, "CUDA out of memory")  # type: ignore[attr-defined]
        dataset_dir, config_path, output_dir = self._setup_env(tmp_path)

        result = run_fine_tune("test/project", dataset_dir, config_path, output_dir, version=1)
        assert result.success is False
        assert "CUDA out of memory" in result.error

    def test_validation_failure_without_force(self, tmp_path: Path) -> None:
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()

        metadata = {"total_examples": 10, "structure_counts": {"other": 10}}
        (dataset_dir / "metadata.json").write_text(json.dumps(metadata))

        config_path = dataset_dir / "config.yaml"
        config_path.write_text("base_model: test\n")

        result = run_fine_tune(
            "test/project",
            dataset_dir,
            config_path,
            tmp_path / "out",
            version=1,
        )
        assert result.success is False
        assert "Not enough" in result.error

    @patch("franktheunicorn.fine_tuning.trainer._run_axolotl_local")
    def test_force_bypasses_validation(self, mock_run: object, tmp_path: Path) -> None:
        mock_run.return_value = (True, "Done")  # type: ignore[attr-defined]
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()

        metadata = {"total_examples": 10, "structure_counts": {"other": 10}}
        (dataset_dir / "metadata.json").write_text(json.dumps(metadata))

        config_path = dataset_dir / "config.yaml"
        config_path.write_text("base_model: test\n")

        result = run_fine_tune(
            "test/project",
            dataset_dir,
            config_path,
            tmp_path / "out",
            version=1,
            force=True,
        )
        assert result.success is True

    def test_eval_only_skips_training(self, tmp_path: Path) -> None:
        dataset_dir, config_path, output_dir = self._setup_env(tmp_path)

        result = run_fine_tune(
            "test/project",
            dataset_dir,
            config_path,
            output_dir,
            version=1,
            eval_only=True,
        )
        # Eval-only doesn't run training, so success stays False
        # but no error either.
        assert result.error == ""
        assert result.version == 1
        assert (output_dir / "training_metadata.json").exists()

    @patch("franktheunicorn.fine_tuning.trainer._run_axolotl_docker")
    def test_docker_mode(self, mock_docker: object, tmp_path: Path) -> None:
        mock_docker.return_value = (True, "Docker training complete")  # type: ignore[attr-defined]
        dataset_dir, config_path, output_dir = self._setup_env(tmp_path)

        result = run_fine_tune(
            "test/project",
            dataset_dir,
            config_path,
            output_dir,
            version=1,
            use_docker=True,
        )
        assert result.success is True
        mock_docker.assert_called_once()  # type: ignore[attr-defined]
