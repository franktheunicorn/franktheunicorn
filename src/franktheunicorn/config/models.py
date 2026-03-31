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


KNOWN_LLM_PROVIDERS: frozenset[str] = frozenset({"stub", "claude", "openai", "gemini", "ollama"})


class LLMBackendConfig(BaseModel):
    """Config for which LLM backend to use for review generation."""

    provider: str = "stub"
    model: str = ""
    api_key_env: str = ""
    base_url: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096

    @field_validator("provider")
    @classmethod
    def provider_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_LLM_PROVIDERS:
            logger.warning(
                "Unknown LLM provider '%s'; known values: %s",
                v,
                ", ".join(sorted(KNOWN_LLM_PROVIDERS)),
            )
        return v

    @field_validator("temperature")
    @classmethod
    def temperature_in_range(cls, v: float) -> float:
        if not 0.0 <= v <= 2.0:
            msg = "temperature must be between 0.0 and 2.0"
            raise ValueError(msg)
        return v

    @field_validator("max_tokens")
    @classmethod
    def max_tokens_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "max_tokens must be positive"
            raise ValueError(msg)
        return v


class SupportedAgentConfig(BaseModel):
    """Config for a supported AI agent type in direct feedback."""

    name: str = ""
    session_pattern: str = ""
    feedback_method: str = "url-open"  # "url-open" or "api"
    api_endpoint_env: str = ""


class AgentFeedbackConfig(BaseModel):
    """Config for direct agent feedback channel (v1.25)."""

    direct_session_enabled: bool = True
    supported_agents: list[SupportedAgentConfig] = Field(default_factory=list)


class OperatorConfig(BaseModel):
    """Top-level operator config loaded from operator.yaml."""

    github_username: str = ""
    review_style: str = "direct but kind"
    personality: str = "frank"
    auto_post: bool = False
    poll_interval_seconds: int | None = None
    digest_email: str = ""
    digest_enabled: bool = False
    workspaces: dict[str, object] = Field(default_factory=dict)
    coderabbit: CodeRabbitConfig = Field(default_factory=CodeRabbitConfig)
    agent_feedback: AgentFeedbackConfig = Field(default_factory=AgentFeedbackConfig)
    # Multiple LLM backends can run in parallel. Each produces findings
    # independently; results are combined and deduped via anti-patterns.
    llm_backends: list[LLMBackendConfig] = Field(default_factory=list)

    # Legacy single-backend field — still accepted for backwards compat.
    # If set and llm_backends is empty, it is promoted into llm_backends.
    llm: LLMBackendConfig | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def migrate_legacy_llm(self) -> OperatorConfig:
        """Promote legacy ``llm:`` config into ``llm_backends`` list."""
        if self.llm is not None and not self.llm_backends:
            self.llm_backends = [self.llm]
            self.llm = None
        return self

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
    custom_scoring_max_boost: int = 30
    watch_keywords: list[str] = Field(default_factory=list)
    collaborator_scores: dict[str, float | None] = Field(default_factory=dict)
    ai_agents: list[str] = Field(default_factory=list)
    committers: list[str] = Field(default_factory=list)
    new_contributor_addendum: str = ""
    enabled: bool = True

    # Copy-pasta detection
    copypasta_enabled: bool = False
    copypasta_min_lines: int = 4
    copypasta_scan_extensions: list[str] = Field(default_factory=lambda: [".py"])
    copypasta_llm_enabled: bool = False

    @field_validator("copypasta_min_lines")
    @classmethod
    def copypasta_min_lines_valid(cls, v: int) -> int:
        if v < 2:
            msg = "copypasta_min_lines must be at least 2"
            raise ValueError(msg)
        return v

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
