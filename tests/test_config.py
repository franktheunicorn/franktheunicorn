"""Tests for YAML config loading and Pydantic models."""

from __future__ import annotations

from pathlib import Path

from franktheunicorn.config.loader import load_operator_config, load_project_configs
from franktheunicorn.config.models import OperatorConfig, ProjectConfig


class TestOperatorConfig:
    def test_defaults(self) -> None:
        config = OperatorConfig()
        assert config.github_username == ""
        assert config.auto_post is False
        assert config.poll_interval_seconds == 300

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
