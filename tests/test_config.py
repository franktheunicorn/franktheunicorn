"""Tests for config loading."""

from __future__ import annotations

import yaml

from franktheunicorn.config import (
    OperatorConfig,
    ProjectConfig,
    load_operator_config,
    load_project_configs,
)


def test_operator_config_defaults():
    op = OperatorConfig(github_login="frank")
    assert op.github_login == "frank"
    assert op.trusted_collaborators == []
    assert op.stale_pr_days == 30


def test_project_config_defaults():
    proj = ProjectConfig(slug="test", repo="owner/repo")
    assert proj.enabled is True
    assert proj.asf_project is False
    assert proj.watched_paths == []


def test_load_operator_config_missing_file(tmp_path):
    op = load_operator_config(tmp_path / "nonexistent.yaml")
    assert op.github_login == "unknown"


def test_load_operator_config_from_yaml(tmp_path):
    cfg = {
        "github_login": "franktheunicorn",
        "email": "frank@example.com",
        "trusted_collaborators": ["alice", "bob"],
        "stale_pr_days": 14,
    }
    config_file = tmp_path / "operator.yaml"
    config_file.write_text(yaml.dump(cfg))

    op = load_operator_config(config_file)
    assert op.github_login == "franktheunicorn"
    assert op.email == "frank@example.com"
    assert "alice" in op.trusted_collaborators
    assert op.stale_pr_days == 14


def test_load_project_configs_empty_dir(tmp_path):
    configs = load_project_configs(tmp_path)
    assert configs == []


def test_load_project_configs_from_yaml(tmp_path):
    cfg = {
        "slug": "myproject",
        "repo": "example/myproject",
        "watched_paths": ["src/core/**"],
        "frequent_contributors": ["carol"],
        "asf_project": False,
    }
    (tmp_path / "myproject.yaml").write_text(yaml.dump(cfg))

    configs = load_project_configs(tmp_path)
    assert len(configs) == 1
    assert configs[0].slug == "myproject"
    assert configs[0].repo == "example/myproject"
    assert "src/core/**" in configs[0].watched_paths


def test_load_multiple_project_configs(tmp_path):
    for i in range(3):
        cfg = {"slug": f"proj{i}", "repo": f"example/proj{i}"}
        (tmp_path / f"proj{i}.yaml").write_text(yaml.dump(cfg))
    configs = load_project_configs(tmp_path)
    assert len(configs) == 3
    slugs = {c.slug for c in configs}
    assert slugs == {"proj0", "proj1", "proj2"}
