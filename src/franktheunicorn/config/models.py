"""Pydantic models for operator and per-project YAML configuration."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, field_validator, model_validator

from franktheunicorn.config.schema import GITHUB_NAME_PATTERN, KNOWN_GOVERNANCE_VALUES

logger = logging.getLogger(__name__)


class CodeRabbitConfig(BaseModel):
    """Config for CodeRabbit CLI integration."""

    enabled: bool = False
    cli_path: str = "coderabbit"
    extra_args: list[str] = Field(default_factory=list)


class OperatorConfig(BaseModel):
    """Top-level operator config loaded from operator.yaml."""

    github_username: str = ""
    review_style: str = "direct but kind"
    auto_post: bool = False
    poll_interval_seconds: int | None = None
    digest_email: str = ""
    digest_enabled: bool = False
    coderabbit: CodeRabbitConfig = Field(default_factory=CodeRabbitConfig)

    @field_validator("poll_interval_seconds")
    @classmethod
    def poll_interval_must_be_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            msg = "poll_interval_seconds must be positive"
            raise ValueError(msg)
        return v

    @field_validator("github_username")
    @classmethod
    def github_username_valid(cls, v: str) -> str:
        v = v.strip()
        if v and not GITHUB_NAME_PATTERN.match(v):
            msg = "github_username contains invalid characters"
            raise ValueError(msg)
        return v


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
    scoring_weights: dict[str, float] = Field(default_factory=dict)
    custom_scoring_expressions: list[str] = Field(default_factory=list)
    watch_keywords: list[str] = Field(default_factory=list)
    collaborator_scores: dict[str, float | None] = Field(default_factory=dict)
    ai_agents: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("owner", "repo")
    @classmethod
    def name_must_be_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            msg = "must not be empty"
            raise ValueError(msg)
        if not GITHUB_NAME_PATTERN.match(v):
            msg = "contains invalid characters"
            raise ValueError(msg)
        return v

    @field_validator("governance")
    @classmethod
    def governance_normalize(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_GOVERNANCE_VALUES:
            logger.warning(
                "Unknown governance value '%s'; known values: %s",
                v,
                ", ".join(sorted(KNOWN_GOVERNANCE_VALUES)),
            )
        return v

    @model_validator(mode="after")
    def warn_overlapping_paths(self) -> ProjectConfig:
        overlap = set(self.watched_paths) & set(self.ignore_paths)
        if overlap:
            logger.warning(
                "Project %s has paths in both watched_paths and ignore_paths: %s",
                self.full_name,
                ", ".join(sorted(overlap)),
            )
        return self

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"
