"""Pydantic models for operator and per-project YAML configuration."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, field_validator, model_validator

from franktheunicorn.config.schema import GITHUB_NAME_PATTERN, KNOWN_GOVERNANCE_VALUES

KNOWN_FORGE_TYPES: frozenset[str] = frozenset({"github", "gitea", "forgejo", "gitlab"})

_DEFAULT_FORGE_BASE_URLS: dict[str, str] = {
    "github": "https://api.github.com",
    "gitlab": "https://gitlab.com",
    "forgejo": "https://codeberg.org",
}

logger = logging.getLogger(__name__)


class CodeRabbitConfig(BaseModel):
    """Config for CodeRabbit CLI integration."""

    enabled: bool = False
    cli_path: str = "coderabbit"
    extra_args: list[str] = Field(default_factory=list)
    deduplicate: bool = True


class JiraConfig(BaseModel):
    """Config for JIRA integration (v1.5)."""

    enabled: bool = False
    server: str = ""
    project_prefix: str = ""

    @field_validator("server")
    @classmethod
    def server_must_be_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v and not v.startswith(("http://", "https://")):
            msg = "JIRA server must be a URL starting with http:// or https://"
            raise ValueError(msg)
        return v


KNOWN_COMMUNITY_SOURCE_TYPES: frozenset[str] = frozenset(
    {"mailing-list", "discourse", "discord", "perplexity", "github-issues", "sentry"}
)


class CommunitySourceConfig(BaseModel):
    """Config for a single community context source (v1.5)."""

    type: str
    name: str = ""
    archive_url: str = ""
    base_url: str = ""
    timeout_seconds: int = 30
    guild_id: str = ""  # Discord-specific
    bot_token_env: str = ""  # Discord-specific
    cache_ttl_days: int = 7
    niceness_delay_seconds: float = 2.0  # delay between requests

    @field_validator("type")
    @classmethod
    def type_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_COMMUNITY_SOURCE_TYPES:
            logger.warning(
                "Unknown community source type '%s'; known: %s",
                v,
                ", ".join(sorted(KNOWN_COMMUNITY_SOURCE_TYPES)),
            )
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def timeout_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "timeout_seconds must be positive"
            raise ValueError(msg)
        return v


class DownstreamConfig(BaseModel):
    """Config for cross-project downstream detection (v1.5)."""

    project: str
    repo: str
    tracked_apis_file: str = ""


class PostingConfig(BaseModel):
    """Config for comment posting mode (v1.5)."""

    mode: str = "draft-only"  # draft-only | confidence-gated
    confidence_threshold: float = 0.85
    bot_token_env: str = "GITHUB_TOKEN_BOT"

    @field_validator("mode")
    @classmethod
    def mode_must_be_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("draft-only", "confidence-gated"):
            msg = "posting mode must be 'draft-only' or 'confidence-gated'"
            raise ValueError(msg)
        return v

    @field_validator("confidence_threshold")
    @classmethod
    def threshold_in_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            msg = "confidence_threshold must be between 0.0 and 1.0"
            raise ValueError(msg)
        return v


class SentryConfig(BaseModel):
    """Config for Sentry integration (v1.5)."""

    enabled: bool = False
    auth_token_env: str = "SENTRY_AUTH_TOKEN"
    org_slug: str = ""
    project_slug: str = ""
    score_weight: int = 15


class PerplexityConfig(BaseModel):
    """Config for Perplexity API integration (v1.5)."""

    enabled: bool = False
    api_key_env: str = "PERPLEXITY_API_KEY"
    mode: str = "both"  # general | technical | both

    @field_validator("mode")
    @classmethod
    def mode_must_be_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("general", "technical", "both"):
            msg = "Perplexity mode must be 'general', 'technical', or 'both'"
            raise ValueError(msg)
        return v


KNOWN_API_MISUSE_REGISTRIES: frozenset[str] = frozenset({"pypi", "maven"})


class APIMisuseConfig(BaseModel):
    """Config for the api-misuse review check.

    Looks up upstream docs for functions called in the diff and asks the LLM
    to flag misuse (complexity-on-large-input, deprecated APIs, ignored
    return values, etc.). Disabled by default; opt in via
    ``llm_checks: ["api-misuse"]`` in the project YAML.
    """

    enabled: bool = False
    registries: list[str] = Field(default_factory=lambda: ["pypi", "maven"])
    cache_ttl_days: int = 7
    max_calls_per_pr: int = 30
    fetch_timeout_seconds: float = 10.0
    # When True, also fetch hosted docs (readthedocs/javadoc.io). When False,
    # use only registry metadata + docstrings (faster, no scraping).
    scrape_hosted_docs: bool = True

    @field_validator("registries")
    @classmethod
    def registries_must_be_known(cls, v: list[str]) -> list[str]:
        normalized = [r.strip().lower() for r in v if r.strip()]
        for r in normalized:
            if r not in KNOWN_API_MISUSE_REGISTRIES:
                logger.warning(
                    "Unknown api-misuse registry '%s'; known: %s",
                    r,
                    ", ".join(sorted(KNOWN_API_MISUSE_REGISTRIES)),
                )
        return normalized

    @field_validator("cache_ttl_days")
    @classmethod
    def cache_ttl_non_negative(cls, v: int) -> int:
        if v < 0:
            msg = "cache_ttl_days must be non-negative"
            raise ValueError(msg)
        return v

    @field_validator("max_calls_per_pr")
    @classmethod
    def max_calls_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "max_calls_per_pr must be positive"
            raise ValueError(msg)
        return v

    @field_validator("fetch_timeout_seconds")
    @classmethod
    def timeout_positive(cls, v: float) -> float:
        if v <= 0:
            msg = "fetch_timeout_seconds must be positive"
            raise ValueError(msg)
        return v


KNOWN_LLM_PROVIDERS: frozenset[str] = frozenset(
    {"stub", "claude", "openai", "gemini", "ollama", "llama-cpp", "vllm"}
)


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


KNOWN_SCHEDULE_FREQUENCIES: frozenset[str] = frozenset({"daily", "weekly", "monthly"})

KNOWN_QUANTIZATION_MODES: frozenset[str] = frozenset({"qlora-4bit", "qlora-8bit", "lora"})

KNOWN_FT_PROVIDERS: frozenset[str] = frozenset(
    {"ollama", "vllm", "llama-cpp", "modal", "runpod", "together"}
)

KNOWN_FT_SLOTS: frozenset[str] = frozenset({"first-pass", "fast", "primary", "reasoning"})

KNOWN_MERGE_METHODS: frozenset[str] = frozenset({"merge", "squash", "rebase"})


class AutoScheduleConfig(BaseModel):
    """Config for automatic fine-tuning scheduling (v2)."""

    enabled: bool = False
    check_frequency: str = "weekly"
    min_new_actions: int = 50
    notify_on_completion: bool = True

    @field_validator("check_frequency")
    @classmethod
    def frequency_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_SCHEDULE_FREQUENCIES:
            msg = f"check_frequency must be one of: {', '.join(sorted(KNOWN_SCHEDULE_FREQUENCIES))}"
            raise ValueError(msg)
        return v

    @field_validator("min_new_actions")
    @classmethod
    def min_actions_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "min_new_actions must be positive"
            raise ValueError(msg)
        return v


class DatasetRefreshConfig(BaseModel):
    """Config for incremental training data refresh (v2)."""

    enabled: bool = True
    frequency: str = "daily"

    @field_validator("frequency")
    @classmethod
    def frequency_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_SCHEDULE_FREQUENCIES:
            msg = f"frequency must be one of: {', '.join(sorted(KNOWN_SCHEDULE_FREQUENCIES))}"
            raise ValueError(msg)
        return v


class FineTuningConfig(BaseModel):
    """Config for fine-tuning pipeline (v2 — Tier 3 learning)."""

    enabled: bool = False
    default_base_model: str = "Qwen/Qwen2.5-Coder-7B-Instruct"
    quantization: str = "qlora-4bit"
    target_hardware: str = "3090"
    auto_schedule: AutoScheduleConfig = Field(default_factory=AutoScheduleConfig)
    dataset_refresh: DatasetRefreshConfig = Field(default_factory=DatasetRefreshConfig)

    @field_validator("quantization")
    @classmethod
    def quantization_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_QUANTIZATION_MODES:
            logger.warning(
                "Unknown quantization mode '%s'; known: %s",
                v,
                ", ".join(sorted(KNOWN_QUANTIZATION_MODES)),
            )
        return v


class FineTunedModelConfig(BaseModel):
    """Config for a deployed fine-tuned model on a project (v2)."""

    enabled: bool = False
    provider: str = "ollama"
    model: str = ""
    endpoint: str = "http://localhost:11434"
    slot: str = "first-pass"
    refine_with: str = "primary"

    @field_validator("provider")
    @classmethod
    def provider_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_FT_PROVIDERS:
            logger.warning(
                "Unknown fine-tuned model provider '%s'; known: %s",
                v,
                ", ".join(sorted(KNOWN_FT_PROVIDERS)),
            )
        return v

    @field_validator("slot")
    @classmethod
    def slot_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_FT_SLOTS:
            msg = f"slot must be one of: {', '.join(sorted(KNOWN_FT_SLOTS))}"
            raise ValueError(msg)
        return v


class MergeQueueConfig(BaseModel):
    """Config for merge queue tracking and execution (v2)."""

    enabled: bool = False
    required_approvals: int = 1
    require_ci_pass: bool = True
    require_no_conflicts: bool = True
    merge_script: str = ""
    auto_merge: bool = False
    merge_method: str = "merge"
    post_merge_restack_enabled: bool = False
    restack_target_branch: str = "main"
    stale_migration_strategy: str = "app-local-diff"
    restack_commit_scope: str = "merge-queue"

    @field_validator("required_approvals")
    @classmethod
    def approvals_non_negative(cls, v: int) -> int:
        if v < 0:
            msg = "required_approvals must be non-negative"
            raise ValueError(msg)
        return v

    @field_validator("merge_method")
    @classmethod
    def merge_method_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_MERGE_METHODS:
            msg = f"merge_method must be one of: {', '.join(sorted(KNOWN_MERGE_METHODS))}"
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


class EmailConfig(BaseModel):
    """Config for email digest delivery.

    Secret fields (``smtp_pass``) should use ``${ENV_VAR}`` syntax in
    YAML so the actual secret is never stored in config files.
    """

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""  # typically "${REVIEW_AGENT_SMTP_PASS}"
    from_address: str = "frank@localhost"
    use_tls: bool = True

    @field_validator("smtp_port")
    @classmethod
    def port_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "smtp_port must be positive"
            raise ValueError(msg)
        return v


class SecurityEmailConfig(BaseModel):
    """Config for security report email inbox (IMAP).

    Secret fields should use ``${ENV_VAR}`` syntax.
    """

    enabled: bool = False
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_pass: str = ""  # typically "${SECURITY_EMAIL_PASS}"
    use_ssl: bool = True
    folder: str = "INBOX"
    poll_interval_seconds: int = 300

    @field_validator("imap_port")
    @classmethod
    def port_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "imap_port must be positive"
            raise ValueError(msg)
        return v

    @field_validator("poll_interval_seconds")
    @classmethod
    def poll_interval_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "poll_interval_seconds must be positive"
            raise ValueError(msg)
        return v


class SecurityTriageConfig(BaseModel):
    """Config for security report triage feature."""

    enabled: bool = False
    email: SecurityEmailConfig = Field(default_factory=SecurityEmailConfig)
    nvd_api_key_env: str = ""  # optional, for higher NVD rate limits
    auto_triage: bool = True  # automatically run LLM triage on new reports
    sandbox_enabled: bool = False  # allow sandbox POC execution


class ForgeRegistryEntry(BaseModel):
    """A single forge instance the operator wants to talk to.

    Each project YAML references one of these by ``name``. ``type`` selects
    the client implementation (``github``, ``gitea``, ``forgejo``, ``gitlab``).
    Gitea and Forgejo share the same API and use the same client.
    """

    name: str
    type: str = "github"
    base_url: str = ""
    token: str = ""
    username: str = ""

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            msg = "forge entry name must not be empty"
            raise ValueError(msg)
        return v

    @field_validator("type")
    @classmethod
    def type_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_FORGE_TYPES:
            msg = f"unknown forge type {v!r}; must be one of {', '.join(sorted(KNOWN_FORGE_TYPES))}"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def fill_default_base_url(self) -> ForgeRegistryEntry:
        """Apply the canonical base URL for the forge type, if unset.

        Gitea has no canonical hosted instance, so a base_url is required.
        """
        if not self.base_url:
            default = _DEFAULT_FORGE_BASE_URLS.get(self.type, "")
            if default:
                self.base_url = default
            elif self.type == "gitea":
                msg = f"forge {self.name!r} (type=gitea) requires base_url"
                raise ValueError(msg)
        return self


class OperatorConfig(BaseModel):
    """Top-level operator config loaded from operator.yaml."""

    github_username: str = ""
    review_style: str = "direct but kind"
    personality: str = "frank"
    auto_post: bool = False
    poll_interval_seconds: int | None = None
    log_level: str = "INFO"
    digest_email: str = ""
    digest_enabled: bool = False
    workspaces: dict[str, object] = Field(default_factory=dict)
    coderabbit: CodeRabbitConfig = Field(default_factory=CodeRabbitConfig)
    agent_feedback: AgentFeedbackConfig = Field(default_factory=AgentFeedbackConfig)
    sentry: SentryConfig = Field(default_factory=SentryConfig)
    perplexity: PerplexityConfig = Field(default_factory=PerplexityConfig)
    fine_tuning: FineTuningConfig = Field(default_factory=FineTuningConfig)
    security_triage: SecurityTriageConfig = Field(default_factory=SecurityTriageConfig)
    # Multiple LLM backends can run in parallel. Each produces findings
    # independently; results are combined and deduped via anti-patterns.
    llm_backends: list[LLMBackendConfig] = Field(default_factory=list)

    # Legacy single-backend field — still accepted for backwards compat.
    # If set and llm_backends is empty, it is promoted into llm_backends.
    llm: LLMBackendConfig | None = Field(default=None, exclude=True)

    # --- Unified config fields (formerly in .env) ---
    # These make operator.yaml the single source of truth.
    # Secret values should use ${ENV_VAR} syntax.
    mock_mode: bool = False
    data_dir: str = ""  # empty = default (BASE_DIR/data)
    fixtures_dir: str = ""  # empty = default (config/fixtures)
    repos_dir: str = ""  # empty = default (data/repos)
    projects_dir: str = ""  # empty = default (config/active/projects)
    github_token: str = ""  # typically "${FRANK_GITHUB_TOKEN}"
    email: EmailConfig = Field(default_factory=EmailConfig)

    # Multi-forge registry. Each entry is a named forge instance (a GitHub
    # account, a Codeberg account, a self-hosted Gitea/GitLab, ...). Project
    # YAMLs reference an entry by ``name`` via their ``forge:`` field. If
    # left empty, a default ``github`` entry is synthesized from the legacy
    # ``github_token``/``github_username`` fields.
    forges: list[ForgeRegistryEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def migrate_legacy_llm(self) -> OperatorConfig:
        """Promote legacy ``llm:`` config into ``llm_backends`` list."""
        if self.llm is not None and not self.llm_backends:
            self.llm_backends = [self.llm]
            self.llm = None
        return self

    @model_validator(mode="after")
    def synthesize_default_forge(self) -> OperatorConfig:
        """Auto-create a ``github`` forge entry from legacy fields if missing.

        Preserves backward compatibility with operator.yaml files written
        before the multi-forge registry existed.
        """
        if not self.forges and self.github_token:
            self.forges = [
                ForgeRegistryEntry(
                    name="github",
                    type="github",
                    base_url=_DEFAULT_FORGE_BASE_URLS["github"],
                    token=self.github_token,
                    username=self.github_username,
                )
            ]
        return self

    @model_validator(mode="after")
    def forge_names_unique(self) -> OperatorConfig:
        """Reject duplicate forge ``name`` entries — projects pick by name."""
        seen: set[str] = set()
        for entry in self.forges:
            if entry.name in seen:
                msg = f"duplicate forge name in registry: {entry.name!r}"
                raise ValueError(msg)
            seen.add(entry.name)
        return self

    @model_validator(mode="after")
    def forge_tokens_set(self) -> OperatorConfig:
        """Fail fast when a forge entry's token resolved to empty.

        Tokens come from ``${VAR}`` substitution at YAML load time; an
        empty value almost always means the referenced env var is not
        set. Surface that here rather than waiting for a 401 from the
        forge API. Bypassed when ``mock_mode`` is true.
        """
        if self.mock_mode:
            return self
        missing = [e.name for e in self.forges if not e.token]
        if missing:
            msg = (
                f"forge entries with empty token (env var likely unset): "
                f"{', '.join(missing)}. "
                f"Set the referenced ${{...}} variables in .env, or enable "
                f"mock_mode for offline use."
            )
            raise ValueError(msg)
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

    @field_validator("log_level")
    @classmethod
    def log_level_valid(cls, v: str) -> str:
        v = (v or "INFO").strip().upper()
        valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if v not in valid:
            msg = f"log_level must be one of {sorted(valid)}, got {v!r}"
            raise ValueError(msg)
        return v


class ContextConfig(BaseModel):
    """Optional full-file and first-party-import context for review prompts.

    When enabled, the drafter reads the local checkout and includes the full
    contents of changed files (when they fit ``per_file_token_cap``) and the
    first-party modules they import — up to ``total_token_budget`` total.
    Tokens are estimated cheaply as ``len(text) // 4``; the budget leaves
    headroom for that approximation.
    """

    include_full_file: bool = True
    include_first_party_imports: bool = True
    total_token_budget: int = 4000
    per_file_token_cap: int = 2000
    import_depth: int = 1
    package_roots: list[str] = Field(default_factory=list)

    @field_validator("total_token_budget", "per_file_token_cap")
    @classmethod
    def budget_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "must be positive"
            raise ValueError(msg)
        return v

    @field_validator("import_depth")
    @classmethod
    def import_depth_in_range(cls, v: int) -> int:
        if v < 0:
            msg = "import_depth must be >= 0"
            raise ValueError(msg)
        if v > 1:
            logger.warning(
                "import_depth=%d requested; v1 resolver only walks one level (treating as 1)",
                v,
            )
        return v


class TestAutoBuildConfig(BaseModel):
    """Auto-build instructions used when no prebuilt image or Dockerfile is given."""

    __test__ = False  # not a pytest test class

    base_image: str = "python:3.12-slim"
    requirements_files: list[str] = Field(default_factory=list)
    setup_commands: list[str] = Field(default_factory=list)


_KNOWN_TEST_RESOURCE_TIERS = {"heavy", "standard", "light"}


class TestExecutionConfig(BaseModel):
    """Per-project differential test runner config (§9 of master design).

    Three mutually exclusive image sources, checked in order:
      1. ``container_image`` — use a prebuilt image as-is.
      2. ``dockerfile``      — path inside the repo to a Dockerfile to build.
      3. ``auto_build``      — generate a Dockerfile from base + requirements.

    If none are set and ``enabled`` is true, the runner falls back to
    ``python:3.12-slim`` (suitable only for projects with zero deps).
    """

    __test__ = False  # not a pytest test class

    enabled: bool = False
    container_image: str | None = None
    dockerfile: str | None = None
    auto_build: TestAutoBuildConfig | None = None
    resource_tier: str = "standard"
    test_command: str = "python -m pytest {tests} --tb=short -q"
    workdir: str = "/workspace"
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("resource_tier")
    @classmethod
    def resource_tier_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in _KNOWN_TEST_RESOURCE_TIERS:
            msg = f"resource_tier must be one of {sorted(_KNOWN_TEST_RESOURCE_TIERS)}, got {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("workdir")
    @classmethod
    def workdir_must_be_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            msg = "workdir must be an absolute path"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def exactly_one_image_source(self) -> TestExecutionConfig:
        sources = [
            ("container_image", self.container_image is not None),
            ("dockerfile", self.dockerfile is not None),
            ("auto_build", self.auto_build is not None),
        ]
        set_sources = [name for name, present in sources if present]
        if len(set_sources) > 1:
            msg = (
                "tests: only one of container_image, dockerfile, auto_build "
                f"may be set (got: {', '.join(set_sources)})"
            )
            raise ValueError(msg)
        return self


class ProjectConfig(BaseModel):
    """Per-project config loaded from a YAML file in the projects directory."""

    owner: str
    repo: str
    # Name of the forge entry in OperatorConfig.forges to use for this
    # project. Defaults to "github" for backward compatibility.
    forge: str = "github"
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
    cve_files: list[str] = Field(default_factory=list)
    new_contributor_addendum: str = ""
    enabled: bool = True

    # v1.5 features
    jira: JiraConfig = Field(default_factory=JiraConfig)
    community_sources: list[CommunitySourceConfig] = Field(default_factory=list)
    downstream: list[DownstreamConfig] = Field(default_factory=list)
    posting: PostingConfig = Field(default_factory=PostingConfig)

    # Copy-pasta detection
    copypasta_enabled: bool = False
    copypasta_min_lines: int = 4
    copypasta_scan_extensions: list[str] = Field(default_factory=lambda: [".py"])
    copypasta_llm_enabled: bool = False

    # v2 features
    fine_tuned_model: FineTunedModelConfig = Field(default_factory=FineTunedModelConfig)
    merge_queue: MergeQueueConfig = Field(default_factory=MergeQueueConfig)

    # LLM sub-checks (v1) — e.g. ["coverage"]
    llm_checks: list[str] = Field(default_factory=list)
    api_misuse: APIMisuseConfig = Field(default_factory=APIMisuseConfig)

    # Full-file + first-party-import context for review prompts (v1).
    context: ContextConfig = Field(default_factory=ContextConfig)

    # Differential test runner (§9). Disabled by default; see docs/test-runner.md.
    tests: TestExecutionConfig = Field(default_factory=TestExecutionConfig)

    @field_validator("llm_checks")
    @classmethod
    def llm_checks_warn_unknown(cls, v: list[str]) -> list[str]:
        known = {
            "api-misuse",
            "coverage",
            "issue-link",
            "malicious-prompt",
            "security",
            "security-context",
        }
        for name in v:
            if name not in known:
                logger.warning(
                    "Unknown llm_check '%s'; known checks: %s",
                    name,
                    ", ".join(sorted(known)),
                )
        return v

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
