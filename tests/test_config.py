"""Tests for YAML config loading, validation, and Pydantic models."""

from __future__ import annotations

import logging
from pathlib import Path
import pytest
from pydantic import ValidationError

from franktheunicorn.config.loader import (
    get_operator_config,
    get_project_config,
    load_operator_config,
    load_project_configs,
)
from franktheunicorn.config.models import OperatorConfig, ProjectConfig
from franktheunicorn.config.schema import validate_yaml_file


# ---------------------------------------------------------------------------
# Model defaults
# ---------------------------------------------------------------------------


class TestOperatorConfig:
    def test_defaults(self) -> None:
        config = OperatorConfig()
        assert config.github_username == ""
        assert config.auto_post is False
        assert config.poll_interval_seconds is None

    def test_from_values(self) -> None:
        config = OperatorConfig(
            github_username="holdenk",
            review_style="direct but kind",
        )
        assert config.github_username == "holdenk"
        assert config.review_style == "direct but kind"


class TestProjectConfig:
    def test_defaults(self) -> None:
        config = ProjectConfig(owner="apache", repo="spark")
        assert config.full_name == "apache/spark"
        assert config.enabled is True
        assert config.watched_paths == []

    def test_full_config(self) -> None:
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            watched_paths=["sql/catalyst/"],
            frequent_contributors=["cloud-fan"],
            governance="asf",
        )
        assert config.full_name == "apache/spark"
        assert "sql/catalyst/" in config.watched_paths
        assert config.governance == "asf"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestOperatorConfigValidation:
    def test_negative_poll_interval_rejected(self) -> None:
        with pytest.raises(ValidationError, match="poll_interval_seconds must be positive"):
            OperatorConfig(poll_interval_seconds=-1)

    def test_zero_poll_interval_rejected(self) -> None:
        with pytest.raises(ValidationError, match="poll_interval_seconds must be positive"):
            OperatorConfig(poll_interval_seconds=0)

    def test_positive_poll_interval_accepted(self) -> None:
        config = OperatorConfig(poll_interval_seconds=60)
        assert config.poll_interval_seconds == 60

    def test_none_poll_interval_accepted(self) -> None:
        config = OperatorConfig()
        assert config.poll_interval_seconds is None

    def test_invalid_github_username_rejected(self) -> None:
        with pytest.raises(ValidationError, match="invalid characters"):
            OperatorConfig(github_username="has spaces")

    def test_empty_github_username_accepted(self) -> None:
        config = OperatorConfig(github_username="")
        assert config.github_username == ""

    def test_whitespace_username_stripped(self) -> None:
        config = OperatorConfig(github_username="  holdenk  ")
        assert config.github_username == "holdenk"


class TestProjectConfigValidation:
    def test_empty_owner_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            ProjectConfig(owner="", repo="spark")

    def test_empty_repo_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            ProjectConfig(owner="apache", repo="")

    def test_invalid_owner_pattern_rejected(self) -> None:
        with pytest.raises(ValidationError, match="invalid characters"):
            ProjectConfig(owner="has spaces", repo="spark")

    def test_unknown_governance_accepted_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            config = ProjectConfig(owner="x", repo="y", governance="Unknown")
        assert config.governance == "unknown"  # lowercased
        assert "Unknown governance value" in caplog.text

    def test_known_governance_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            ProjectConfig(owner="x", repo="y", governance="ASF")
        assert "Unknown governance value" not in caplog.text

    def test_overlapping_paths_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            ProjectConfig(
                owner="x",
                repo="y",
                watched_paths=["src/", "docs/"],
                ignore_paths=["docs/"],
            )
        assert "watched_paths and ignore_paths" in caplog.text
        assert "docs/" in caplog.text

    def test_no_overlap_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            ProjectConfig(
                owner="x",
                repo="y",
                watched_paths=["src/"],
                ignore_paths=["docs/"],
            )
        assert "watched_paths and ignore_paths" not in caplog.text


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


class TestConfigLoader:
    def test_load_operator_config_from_file(self, tmp_config_dir: Path) -> None:
        config = load_operator_config(tmp_config_dir / "operator.yaml")
        assert config.github_username == "testuser"
        assert config.poll_interval_seconds == 60

    def test_load_operator_config_missing_file(self, tmp_path: Path) -> None:
        config = load_operator_config(tmp_path / "nonexistent.yaml")
        assert config.github_username == ""  # defaults

    def test_load_project_configs(self, tmp_config_dir: Path) -> None:
        configs = load_project_configs(tmp_config_dir / "projects")
        assert len(configs) == 1
        assert configs[0].owner == "testorg"
        assert configs[0].repo == "testrepo"
        assert "src/" in configs[0].watched_paths

    def test_load_project_configs_missing_dir(self, tmp_path: Path) -> None:
        configs = load_project_configs(tmp_path / "nonexistent")
        assert configs == []


# ---------------------------------------------------------------------------
# YAML error handling
# ---------------------------------------------------------------------------


class TestYAMLErrorHandling:
    def test_load_operator_config_invalid_yaml(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text(": invalid: yaml: [")
        config = load_operator_config(bad_file)
        assert config.github_username == ""  # fell back to defaults

    def test_load_operator_config_validation_error(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("poll_interval_seconds: -5\n")
        config = load_operator_config(bad_file)
        assert config.poll_interval_seconds is None  # fell back to defaults

    def test_load_project_configs_skips_invalid(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        good = projects_dir / "good.yaml"
        good.write_text("owner: apache\nrepo: spark\n")

        bad = projects_dir / "bad.yaml"
        bad.write_text(": invalid: yaml: [")

        configs = load_project_configs(projects_dir)
        assert len(configs) == 1
        assert configs[0].owner == "apache"

    def test_load_project_configs_skips_validation_error(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        good = projects_dir / "good.yaml"
        good.write_text("owner: apache\nrepo: spark\n")

        bad = projects_dir / "invalid.yaml"
        bad.write_text("owner: ''\nrepo: spark\n")

        configs = load_project_configs(projects_dir)
        assert len(configs) == 1
        assert configs[0].owner == "apache"


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


class TestConvenienceFunctions:
    def test_get_operator_config(self, tmp_config_dir: Path, settings: object) -> None:
        settings.FRANK_OPERATOR_CONFIG = str(tmp_config_dir / "operator.yaml")  # type: ignore[attr-defined]
        config = get_operator_config()
        assert config.github_username == "testuser"

    def test_get_project_config_by_name(self, tmp_config_dir: Path, settings: object) -> None:
        settings.FRANK_PROJECTS_DIR = str(tmp_config_dir / "projects")  # type: ignore[attr-defined]
        config = get_project_config("testorg-testrepo")
        assert config is not None
        assert config.owner == "testorg"

    def test_get_project_config_by_full_name(self, tmp_config_dir: Path, settings: object) -> None:
        settings.FRANK_PROJECTS_DIR = str(tmp_config_dir / "projects")  # type: ignore[attr-defined]
        config = get_project_config("testorg/testrepo")
        assert config is not None
        assert config.repo == "testrepo"

    def test_get_project_config_not_found(self, tmp_config_dir: Path, settings: object) -> None:
        settings.FRANK_PROJECTS_DIR = str(tmp_config_dir / "projects")  # type: ignore[attr-defined]
        config = get_project_config("nonexistent")
        assert config is None


# ---------------------------------------------------------------------------
# Standalone YAML validation
# ---------------------------------------------------------------------------


class TestValidateYamlFile:
    def test_valid_operator_yaml(self, tmp_config_dir: Path) -> None:
        errors = validate_yaml_file(tmp_config_dir / "operator.yaml", "operator")
        assert errors == []

    def test_valid_project_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "project.yaml"
        f.write_text("owner: apache\nrepo: spark\n")
        errors = validate_yaml_file(f, "project")
        assert errors == []

    def test_missing_file(self, tmp_path: Path) -> None:
        errors = validate_yaml_file(tmp_path / "nope.yaml", "operator")
        assert len(errors) == 1
        assert "not found" in errors[0].lower()

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text(": invalid: yaml: [")
        errors = validate_yaml_file(f, "operator")
        assert len(errors) == 1
        assert "yaml" in errors[0].lower()

    def test_invalid_operator_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text("poll_interval_seconds: -5\n")
        errors = validate_yaml_file(f, "operator")
        assert len(errors) >= 1

    def test_invalid_project_yaml_missing_required(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text("tone: friendly\n")  # missing owner and repo
        errors = validate_yaml_file(f, "project")
        assert len(errors) >= 1

    def test_not_a_mapping(self, tmp_path: Path) -> None:
        f = tmp_path / "list.yaml"
        f.write_text("- item1\n- item2\n")
        errors = validate_yaml_file(f, "operator")
        assert any("mapping" in e.lower() for e in errors)
