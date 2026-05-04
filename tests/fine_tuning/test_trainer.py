"""Tests for fine-tuning training execution (v2 — §10.4)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from franktheunicorn.fine_tuning.trainer import (
    _run_axolotl_docker,
    _run_axolotl_local,
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


class TestRunAxolotlLocal:
    """Unit tests for the _run_axolotl_local helper."""

    def test_success_returns_stdout(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("base_model: test\n")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Training complete\nLoss: 0.01"

        with patch("subprocess.run", return_value=mock_result):
            ok, log = _run_axolotl_local(config_path)

        assert ok is True
        assert "Training complete" in log

    def test_nonzero_returncode_returns_failure(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("base_model: test\n")
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "CUDA out of memory"

        with patch("subprocess.run", return_value=mock_result):
            ok, log = _run_axolotl_local(config_path)

        assert ok is False
        assert "CUDA out of memory" in log
        assert "exit 1" in log

    def test_file_not_found_returns_helpful_message(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("base_model: test\n")

        with patch("subprocess.run", side_effect=FileNotFoundError("python not found")):
            ok, log = _run_axolotl_local(config_path)

        assert ok is False
        assert "Axolotl not installed" in log

    def test_timeout_returns_error(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("base_model: test\n")

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="python", timeout=3600),
        ):
            ok, log = _run_axolotl_local(config_path)

        assert ok is False
        assert "timed out" in log


class TestRunAxolotlDocker:
    """Unit tests for the _run_axolotl_docker helper."""

    def test_no_docker_returns_error(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("base_model: test\n")
        dataset_dir = tmp_path / "data"
        dataset_dir.mkdir()

        with patch("shutil.which", return_value=None):
            ok, log = _run_axolotl_docker(config_path, dataset_dir)

        assert ok is False
        assert "Docker not found" in log

    def test_success_returns_stdout(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("base_model: test\n")
        dataset_dir = tmp_path / "data"
        dataset_dir.mkdir()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Docker training complete"

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("subprocess.run", return_value=mock_result),
        ):
            ok, log = _run_axolotl_docker(config_path, dataset_dir)

        assert ok is True
        assert "Docker training complete" in log

    def test_docker_failure_returns_error(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("base_model: test\n")
        dataset_dir = tmp_path / "data"
        dataset_dir.mkdir()
        mock_result = MagicMock()
        mock_result.returncode = 2
        mock_result.stderr = "GPU not found"

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("subprocess.run", return_value=mock_result),
        ):
            ok, log = _run_axolotl_docker(config_path, dataset_dir)

        assert ok is False
        assert "GPU not found" in log
        assert "exit 2" in log

    def test_docker_timeout_returns_error(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("base_model: test\n")
        dataset_dir = tmp_path / "data"
        dataset_dir.mkdir()

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=3600),
            ),
        ):
            ok, log = _run_axolotl_docker(config_path, dataset_dir)

        assert ok is False
        assert "timed out" in log


class TestValidateDatasetEdgeCases:
    """Additional validation edge cases."""

    def test_single_structure_type_warning(self, tmp_path: Path) -> None:
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()
        metadata = {
            "total_examples": 300,
            "structure_counts": {"praise-suggestion": 300},
        }
        (dataset_dir / "metadata.json").write_text(json.dumps(metadata))

        result = validate_dataset(dataset_dir)
        # High enough approval rate (300 non-critique / 300 total = 1.0), so still valid
        assert result.valid is True
        assert any("Warning" in e for e in result.errors)

    def test_zero_total_examples(self, tmp_path: Path) -> None:
        dataset_dir = tmp_path / "dataset"
        dataset_dir.mkdir()
        metadata = {"total_examples": 0, "structure_counts": {}}
        (dataset_dir / "metadata.json").write_text(json.dumps(metadata))

        result = validate_dataset(dataset_dir)
        assert result.valid is False
        assert result.approval_rate == 0.0
