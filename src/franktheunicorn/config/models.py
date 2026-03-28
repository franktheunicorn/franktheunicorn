"""Pydantic models for operator and per-project YAML configuration."""

from __future__ import annotations

from pydantic import BaseModel, Field


class OperatorConfig(BaseModel):
    """Top-level operator config loaded from operator.yaml."""

    github_username: str = ""
    review_style: str = "direct but kind"
    auto_post: bool = False
    poll_interval_seconds: int = 300
    digest_email: str = ""
    digest_enabled: bool = False


class ProjectConfig(BaseModel):
    """Per-project config loaded from a YAML file in the projects directory."""

    owner: str
    repo: str
    review_context: str = "general open-source"
    watched_paths: list[str] = Field(default_factory=list)
    ignore_paths: list[str] = Field(default_factory=list)
    tone: str = "direct"
    test_expectations: str = "tests expected for new features"
    frequent_contributors: list[str] = Field(default_factory=list)
    governance: str = "standard"
    enabled: bool = True

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"
