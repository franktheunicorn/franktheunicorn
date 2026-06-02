"""Tests for AgentToolsConfig validation and defaults."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from franktheunicorn.config.models import AgentToolsConfig, ProjectConfig


class TestAgentToolsConfig:
    def test_defaults_off(self) -> None:
        cfg = AgentToolsConfig()
        assert cfg.enabled is False
        assert "grep" in cfg.tools
        assert cfg.enable_compile is False
        assert cfg.enable_run_tests is False

    def test_present_on_project_config_default_off(self) -> None:
        pc = ProjectConfig(owner="o", repo="r")
        assert pc.agent_tools.enabled is False

    def test_unknown_tools_dropped(self) -> None:
        cfg = AgentToolsConfig(tools=["grep", "bogus", "read_file"])
        assert "bogus" not in cfg.tools
        assert cfg.tools == ["grep", "read_file"]

    def test_invalid_resource_tier_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentToolsConfig(resource_tier="gigantic")

    def test_resource_tier_normalized(self) -> None:
        assert AgentToolsConfig(resource_tier="LIGHT").resource_tier == "light"

    def test_compile_requires_build_command(self) -> None:
        with pytest.raises(ValidationError, match="build_command"):
            AgentToolsConfig(enable_compile=True)

    def test_compile_with_command_ok(self) -> None:
        cfg = AgentToolsConfig(enable_compile=True, build_command="make")
        assert cfg.build_command == "make"

    def test_non_positive_budgets_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentToolsConfig(max_iterations=0)
        with pytest.raises(ValidationError):
            AgentToolsConfig(time_budget_seconds=-1)
