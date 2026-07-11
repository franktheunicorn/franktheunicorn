"""Pydantic models for operator and per-project YAML configuration."""

from __future__ import annotations

import logging
import shlex
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from franktheunicorn.config.schema import GITHUB_NAME_PATTERN, KNOWN_GOVERNANCE_VALUES

KNOWN_FORGE_TYPES: frozenset[str] = frozenset({"github", "gitea", "forgejo", "gitlab"})

_DEFAULT_FORGE_BASE_URLS: dict[str, str] = {
    "github": "https://api.github.com",
    "gitlab": "https://gitlab.com",
    "forgejo": "https://codeberg.org",
}

logger = logging.getLogger(__name__)


_KNOWN_REMOTE_MODES: frozenset[str] = frozenset({"local", "ssh"})


class RemoteExecutionConfig(BaseModel):
    """Where to execute a CLI review tool — locally or on a remote SSH host.

    When ``mode == "ssh"``, the worker SSHs to ``host`` and clones the
    project's git repo into ``remote_workspace_dir`` (one path per
    owner/repo) before invoking the CLI there. Subsequent runs ``git fetch``
    instead of re-cloning. The remote host is responsible for having the
    CLI tool installed and any required credentials.
    """

    mode: str = "local"
    host: str = ""
    # Optional TCP port. 0 means "no -p flag" (use ssh's default / ~/.ssh/config).
    # When set, emitted as ``-p <port>`` in the ssh argv.
    port: int = 0
    user: str = ""
    ssh_key_path: str = ""
    ssh_extra_args: list[str] = Field(default_factory=list)
    # Some companies wrap ssh in a custom helper (corp-ssh-helper, assh,
    # teleport's tsh, etc.). ``ssh_command`` is the argv prefix used in
    # place of bare ``ssh`` -- everything else (BatchMode, key path,
    # extra args, target) is appended unchanged.
    ssh_command: list[str] = Field(default_factory=lambda: ["ssh"])
    remote_workspace_dir: str = "~/.frank-remote"
    clone_url_template: str = "https://github.com/{owner}/{repo}.git"
    prepare_timeout_seconds: int = 600

    @field_validator("mode")
    @classmethod
    def mode_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in _KNOWN_REMOTE_MODES:
            msg = f"remote.mode must be one of {sorted(_KNOWN_REMOTE_MODES)}, got {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("remote_workspace_dir")
    @classmethod
    def workspace_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            msg = "remote_workspace_dir must not be empty"
            raise ValueError(msg)
        return v

    @field_validator("ssh_command", mode="before")
    @classmethod
    def ssh_command_normalize(cls, v: object) -> list[str]:
        # Accept a string for ergonomics ("corp-ssh-helper --quiet") and
        # split on whitespace; lists pass through unchanged.
        if isinstance(v, str):
            parts = v.split()
        elif isinstance(v, list):
            parts = [str(p).strip() for p in v if str(p).strip()]
        else:
            msg = "ssh_command must be a string or list of strings"
            raise ValueError(msg)
        if not parts:
            msg = "ssh_command must contain at least one argument"
            raise ValueError(msg)
        return parts

    @field_validator("prepare_timeout_seconds")
    @classmethod
    def prepare_timeout_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "prepare_timeout_seconds must be positive"
            raise ValueError(msg)
        return v

    @field_validator("port")
    @classmethod
    def port_in_range(cls, v: int) -> int:
        if v < 0 or v > 65535:
            msg = f"remote.port must be between 0 and 65535, got {v}"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def host_required_for_ssh(self) -> RemoteExecutionConfig:
        has_custom_command = self.ssh_command != ["ssh"]
        if self.mode == "ssh" and not self.host.strip() and not has_custom_command:
            msg = "remote.host is required when mode='ssh' and no custom ssh_command is set"
            raise ValueError(msg)
        return self


def _parse_cli_path(cli_path: str) -> list[str]:
    """Split a ``cli_path`` string into argv via shell quoting rules.

    Lets operators wrap a CLI in a launcher (``corp-review-runner``,
    ``uv run --with coderabbit coderabbit``, ``docker run --rm
    myorg/coderabbit``, ...) without inventing a separate field. A bare
    binary name still parses to a one-element list, so simple configs
    are unchanged.
    """
    parts = shlex.split(cli_path) if cli_path else []
    if not parts:
        msg = "cli_path must contain at least one argument"
        raise ValueError(msg)
    return parts


def _validate_cli_path(v: str) -> str:
    """Pydantic-friendly validator for ``cli_path`` fields."""
    if not v.strip():
        msg = "cli_path must not be empty"
        raise ValueError(msg)
    try:
        _parse_cli_path(v)
    except ValueError:
        raise
    return v


class CodeRabbitConfig(BaseModel):
    """Config for CodeRabbit CLI integration."""

    enabled: bool = False
    cli_path: str = "coderabbit"
    extra_args: list[str] = Field(default_factory=list)
    deduplicate: bool = True
    remote: RemoteExecutionConfig = Field(default_factory=RemoteExecutionConfig)

    @field_validator("cli_path")
    @classmethod
    def cli_path_parseable(cls, v: str) -> str:
        return _validate_cli_path(v)

    @property
    def cli_argv(self) -> list[str]:
        """``cli_path`` split into argv -- supports ``"cmd arg1 arg2"``."""
        return _parse_cli_path(self.cli_path)


class ClaudeCLIConfig(BaseModel):
    """Config for invoking the Claude CLI as a code reviewer.

    The Claude CLI does not ship a built-in PR-review subcommand, so we
    wrap it in headless prompt mode (``claude -p ...``). Our prompt asks
    Claude to emit findings in the same ``<file>:<line> - [Severity]
    <title>`` block format CodeRabbit produces, so output parsing is
    shared.
    """

    enabled: bool = False
    cli_path: str = "claude"
    model: str = ""
    extra_args: list[str] = Field(default_factory=list)
    timeout_seconds: int = 300
    max_diff_chars: int = 60_000
    remote: RemoteExecutionConfig = Field(default_factory=RemoteExecutionConfig)

    @field_validator("timeout_seconds", "max_diff_chars")
    @classmethod
    def must_be_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "must be positive"
            raise ValueError(msg)
        return v

    @field_validator("cli_path")
    @classmethod
    def cli_path_parseable(cls, v: str) -> str:
        return _validate_cli_path(v)

    @property
    def cli_argv(self) -> list[str]:
        """``cli_path`` split into argv -- supports ``"cmd arg1 arg2"``."""
        return _parse_cli_path(self.cli_path)


class AgentCLIReviewerConfig(BaseModel):
    """Config for a general-purpose agent CLI used as a code reviewer.

    Generalizes :class:`ClaudeCLIConfig`. Any headless coding agent that
    accepts a prompt on the command line and emits free-form text can act
    as a reviewer: we feed it the shared block-format prompt and parse the
    output with the shared parser. The three seeded reviewers are
    ``claude``, ``codex``, and ``pi``; they differ only in how a prompt is
    turned into argv:

    * ``prompt_mode="flag"`` (claude, pi) → ``<cli> [--model M] <extra> -p <prompt>``
    * ``prompt_mode="subcommand"`` (codex) → ``<cli> exec [--model M] <extra> <prompt>``

    ``enabled`` is tri-state: ``True``/``False`` force the reviewer on/off,
    while the default ``"auto"`` means "use it iff its binary is installed"
    (resolved at worker startup — see ``worker.runner``).
    """

    name: str
    enabled: bool | Literal["auto"] = "auto"
    cli_path: str = ""
    model: str = ""
    model_flag: str = "--model"
    prompt_mode: Literal["flag", "subcommand"] = "flag"
    prompt_arg: str = "-p"
    extra_args: list[str] = Field(default_factory=list)
    timeout_seconds: int = 300
    max_diff_chars: int = 60_000
    deduplicate: bool = True
    remote: RemoteExecutionConfig = Field(default_factory=RemoteExecutionConfig)

    @field_validator("timeout_seconds", "max_diff_chars")
    @classmethod
    def must_be_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "must be positive"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def default_cli_path_to_name(self) -> AgentCLIReviewerConfig:
        """Default ``cli_path`` to ``name`` so a bare ``{name: codex}`` works."""
        if not self.cli_path.strip():
            self.cli_path = self.name
        _validate_cli_path(self.cli_path)
        return self

    @property
    def cli_argv(self) -> list[str]:
        """``cli_path`` split into argv -- supports ``"cmd arg1 arg2"``."""
        return _parse_cli_path(self.cli_path)

    def build_invocation(self, prompt: str) -> list[str]:
        """Turn a prompt into the argv suffix appended to ``cli_argv``.

        Handles the model flag, any operator ``extra_args``, and the two
        prompt-delivery styles. For ``subcommand`` mode the subcommand comes
        first and the prompt is the trailing positional argument; for
        ``flag`` mode the prompt follows ``prompt_arg`` (e.g. ``-p``).
        """
        model_part = [self.model_flag, self.model] if self.model else []
        if self.prompt_mode == "subcommand":
            return [self.prompt_arg, *model_part, *self.extra_args, prompt]
        return [*model_part, *self.extra_args, self.prompt_arg, prompt]


def _default_agent_cli_reviewers() -> list[AgentCLIReviewerConfig]:
    """Seed the registry with the three auto-detected agent reviewers.

    Each defaults to ``enabled="auto"`` so it runs only when its binary is
    present on PATH (local mode). Operators can override any entry by name
    in ``operator.yaml`` or add their own agents to the list.
    """
    return [
        AgentCLIReviewerConfig(
            name="claude", cli_path="claude", prompt_mode="flag", prompt_arg="-p"
        ),
        # ``codex exec`` accepts ``-m, --model <MODEL>`` (verified via
        # ``codex exec --help``), so the default ``model_flag="--model"`` works
        # for codex; no override needed.
        AgentCLIReviewerConfig(
            name="codex", cli_path="codex", prompt_mode="subcommand", prompt_arg="exec"
        ),
        AgentCLIReviewerConfig(name="pi", cli_path="pi", prompt_mode="flag", prompt_arg="-p"),
    ]


class SnowflakeReviewConfig(BaseModel):
    """Config for the Snowflake code review CLI integration.

    Mirrors the CodeRabbit shape: invokes ``snowflake-code-review review
    --base-commit <sha> --prompt-only`` and parses the same finding block
    format.
    """

    enabled: bool = False
    cli_path: str = "snowflake-code-review"
    extra_args: list[str] = Field(default_factory=list)
    deduplicate: bool = True
    remote: RemoteExecutionConfig = Field(default_factory=RemoteExecutionConfig)

    @field_validator("cli_path")
    @classmethod
    def cli_path_parseable(cls, v: str) -> str:
        return _validate_cli_path(v)

    @property
    def cli_argv(self) -> list[str]:
        """``cli_path`` split into argv -- supports ``"cmd arg1 arg2"``."""
        return _parse_cli_path(self.cli_path)


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

    # IMAP fields for private/authenticated mailing lists (e.g. Apache private@).
    # When imap_host is set and type is "mailing-list", the IMAP fetcher is used
    # instead of the public lists.apache.org API.
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_pass: str = ""  # use ${ENV_VAR} syntax; expanded at YAML load time by config/loader.py
    imap_folder: str = "INBOX"
    use_ssl: bool = True

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


class BackportConfig(BaseModel):
    """Config for the backport review check.

    When a PR declares itself a backport / cherry-pick of another PR or commit,
    the check fetches the source diff and flags differences from the backport's
    diff. Enable via ``llm_checks: ["backport"]``. The check is deterministic
    (non-LLM).

    ``ignore_paths`` are ``fnmatch`` globs of paths where divergence between the
    source and the backport is expected and should be suppressed (changelogs,
    version bumps, lockfiles, etc.). A trailing slash (or a bare directory
    name) matches everything under that directory: ``"docs/"`` and ``"docs"``
    both ignore ``docs/anything.md``. Plain globs like ``"*.lock"`` or
    ``"CHANGELOG*"`` also work.
    """

    enabled: bool = True
    warn_on_missing_hunks: bool = True
    warn_on_extra_files: bool = True
    warn_on_altered_hunks: bool = True
    ignore_paths: list[str] = Field(default_factory=list)
    # Hard cap on the size (in characters) of the EXTERNALLY-FETCHED SOURCE diff
    # only — the original PR/commit diff this check pulls from the forge. It does
    # NOT cap the PR's own backport diff (the runner already bounds that). A
    # source larger than this short-circuits to a single informational finding
    # instead of being parsed (OOM guard).
    max_source_diff_chars: int = 1_000_000
    # Reserved flag for a future LLM semantic-drift layer. Currently a no-op:
    # setting it True does nothing yet (the deterministic comparison is the
    # only path). Kept so config written against it validates.
    llm_semantic_drift: bool = False

    @field_validator("max_source_diff_chars")
    @classmethod
    def max_source_diff_chars_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "max_source_diff_chars must be positive"
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
    model_config = ConfigDict(populate_by_name=True)

    post_merge_restack_enabled: bool = False
    restack_enabled: bool = Field(default=False, alias="restack")
    restack_target_branch: str = "main"
    migration_globs: list[str] = Field(default_factory=lambda: ["*/migrations/*.py"])
    delete_stale_migrations: bool = True
    ci_wait_timeout_seconds: int = 900
    ci_poll_interval_seconds: int = 30
    push_force_with_lease: bool = True
    stale_migration_strategy: str = "app-local-diff"
    restack_commit_scope: str = "merge-queue"
    # Command used to regenerate migrations during a restack. This runs the
    # *target repo's* project code, so operators should point it at a
    # sandboxed invocation (e.g. ["docker", "run", "--rm", "-v", ...,
    # "img", "python", "manage.py", "makemigrations"]) rather than executing
    # it on the worker host. Kept behind the off-by-default restack flags.
    restack_makemigrations_cmd: list[str] = Field(
        default_factory=lambda: ["python", "manage.py", "makemigrations"]
    )

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

    @field_validator("restack_target_branch")
    @classmethod
    def restack_target_branch_not_empty(cls, v: str) -> str:
        normalized = v.strip()
        if not normalized:
            msg = "restack_target_branch must not be empty"
            raise ValueError(msg)
        return normalized

    @field_validator("migration_globs")
    @classmethod
    def migration_globs_not_empty(cls, v: list[str]) -> list[str]:
        normalized = [glob.strip() for glob in v if glob.strip()]
        if not normalized:
            msg = "migration_globs must contain at least one non-empty glob pattern"
            raise ValueError(msg)
        return normalized

    @field_validator("ci_wait_timeout_seconds")
    @classmethod
    def ci_wait_timeout_in_bounds(cls, v: int) -> int:
        if v < 60 or v > 7200:
            msg = "ci_wait_timeout_seconds must be between 60 and 7200"
            raise ValueError(msg)
        return v

    @field_validator("ci_poll_interval_seconds")
    @classmethod
    def ci_poll_interval_in_bounds(cls, v: int) -> int:
        if v < 5 or v > 300:
            msg = "ci_poll_interval_seconds must be between 5 and 300"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def normalize_restack_flags(self) -> MergeQueueConfig:
        if self.post_merge_restack_enabled:
            self.restack_enabled = True
        if not self.restack_enabled:
            self.delete_stale_migrations = False
        if self.ci_poll_interval_seconds >= self.ci_wait_timeout_seconds:
            msg = "ci_poll_interval_seconds must be lower than ci_wait_timeout_seconds"
            raise ValueError(msg)
        return self


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


class AlertsConfig(BaseModel):
    """Operator-level config for alert mode.

    Alert mode watches for two things: PRs raised by others that overlap
    work the operator has in flight, and security reports sitting in the
    queue or in triage. Alerts are always recorded in the database; email
    delivery additionally requires a recipient (``email`` here, falling
    back to ``digest_email``) and SMTP settings. Missing email config
    degrades gracefully — alerts are recorded but nothing is sent.
    """

    enabled: bool = False
    # Recipient for alert emails. Empty falls back to digest_email; if
    # both are empty, alerts are recorded but no email is sent.
    email: str = ""
    # Alert on security reports in the queue (status "new") or in triage
    # (status "triaging"). Also covers reports not tied to any project.
    security_reports: bool = True


class ProjectAlertsConfig(BaseModel):
    """Per-project config for alert mode.

    Only consulted when the operator-level ``alerts.enabled`` master
    switch is on. ``working_paths``/``working_keywords`` declare what the
    operator is actively working on; PRs by others touching the same
    files as the operator's own open PRs always count as overlap.
    """

    enabled: bool = True
    # Alert when someone else's PR overlaps the operator's in-flight work.
    working_overlap: bool = True
    # Alert on security reports attached to this project.
    security_reports: bool = True
    # Path patterns (glob or prefix, like watched_paths) describing code
    # the operator is actively working on.
    working_paths: list[str] = Field(default_factory=list)
    # Keywords matched against PR title/body (case-insensitive).
    working_keywords: list[str] = Field(default_factory=list)


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
    timeout_seconds: int = 30
    # Optional tag applied to messages ingested as security reports, so the
    # mailbox itself shows what frank has picked up. Applied as a Gmail label
    # (X-GM-LABELS) when the server supports it, else a standard IMAP keyword.
    # Empty (the default) keeps the inbox path fully read-only.
    ingested_tag: str = ""  # e.g. "frank/ingested"

    @field_validator("imap_port")
    @classmethod
    def port_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            msg = "imap_port must be positive"
            raise ValueError(msg)
        return v

    @field_validator("ingested_tag")
    @classmethod
    def ingested_tag_must_be_imap_safe(cls, v: str) -> str:
        v = v.strip()
        # Printable ASCII only, and nothing that would break IMAP quoting.
        # (Spaces are fine — Gmail labels allow them; the keyword fallback
        # sanitizes them.)
        if any(ch in v for ch in ('"', "\\")) or not all(" " <= ch <= "~" for ch in v):
            msg = "ingested_tag must be printable ASCII without quotes or backslashes"
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
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    workspaces: dict[str, object] = Field(default_factory=dict)
    coderabbit: CodeRabbitConfig = Field(default_factory=CodeRabbitConfig)
    # Legacy single Claude-CLI reviewer. Still accepted for backwards compat;
    # promoted into ``agent_cli_reviewers`` below (see assemble_agent_cli_registry).
    claude_cli: ClaudeCLIConfig = Field(default_factory=ClaudeCLIConfig)
    # Generalized agent-CLI reviewer registry. Seeded with claude/codex/pi,
    # each "auto" (runs only when its binary is installed). Operators override
    # an entry by ``name`` or append their own agents.
    agent_cli_reviewers: list[AgentCLIReviewerConfig] = Field(
        default_factory=_default_agent_cli_reviewers
    )
    # Runtime cache for the PATH-resolved agent reviewer set (see
    # worker.runner.resolve_agent_cli_reviewers). Populated once at worker
    # startup so per-PR processing doesn't re-probe ``shutil.which``. Excluded
    # from serialization/equality (PrivateAttr); ``None`` means "not resolved".
    _resolved_agent_cli_reviewers: list[AgentCLIReviewerConfig] | None = PrivateAttr(default=None)
    snowflake_review: SnowflakeReviewConfig = Field(default_factory=SnowflakeReviewConfig)
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
    def assemble_agent_cli_registry(self) -> OperatorConfig:
        """Seed default agent reviewers and promote legacy ``claude_cli``.

        Mirrors the ``llm:`` promotion so v1 configs keep working:

        * Any of the seeded ``claude``/``codex``/``pi`` reviewers missing
          from an operator-supplied list are appended (so a user who lists
          one custom agent still gets auto-detection of the built-ins).
        * A meaningfully-configured legacy ``claude_cli`` block is promoted
          into the registry as the ``claude`` entry, replacing the seed so
          the two never double-run (dedupe by name).
        """
        by_name = {rc.name: rc for rc in self.agent_cli_reviewers}
        for seed in _default_agent_cli_reviewers():
            if seed.name not in by_name:
                self.agent_cli_reviewers.append(seed)
                by_name[seed.name] = seed

        # Promote iff the operator actually provided a ``claude_cli:`` block.
        # ``model_fields_set`` distinguishes "explicitly configured" (even
        # ``claude_cli: {enabled: false}``) from "never set" (the seed default
        # object), so an explicit disable survives as ``enabled=False`` instead
        # of silently reverting to the "auto" seed and auto-running Claude.
        legacy = self.claude_cli
        if "claude_cli" in self.model_fields_set:
            promoted = AgentCLIReviewerConfig(
                name="claude",
                enabled=legacy.enabled,
                cli_path=legacy.cli_path,
                model=legacy.model,
                prompt_mode="flag",
                prompt_arg="-p",
                extra_args=list(legacy.extra_args),
                timeout_seconds=legacy.timeout_seconds,
                max_diff_chars=legacy.max_diff_chars,
                remote=legacy.remote,
            )
            # Replace the seeded "claude" entry in place (dedupe by name).
            self.agent_cli_reviewers = [
                promoted if rc.name == "claude" else rc for rc in self.agent_cli_reviewers
            ]
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
    # Default review-gating policy (token saver). Controls which PRs the
    # expensive LLM review pipeline runs on *automatically* during a poll:
    #   "all"                   — auto-review every ingested PR (pre-gating
    #                             behavior).
    #   "mentioned_or_authored" — only auto-review PRs the operator authored or
    #                             is personally involved in (requested reviewer,
    #                             assignee, or @-mentioned in the PR body). The
    #                             default — on high-volume repos (e.g. Spark)
    #                             this avoids burning tokens reviewing every PR.
    #   "none"                  — never auto-review.
    # This gates ONLY the review pipeline: every PR is still ingested, scored,
    # routed, and shown on the dashboard regardless of policy. The dashboard
    # "Force Run Agents" button (force=True) always bypasses the gate. Configs
    # written before this field existed default to "mentioned_or_authored".
    auto_review_policy: str = "mentioned_or_authored"
    # When True (default), WIP/draft PRs are routed to the "wip" queue and
    # skipped by the review pipeline until they graduate (draft flag cleared,
    # title prefix removed). At that point the normal poll cycle re-routes and
    # processes them. Set to false to review drafts immediately.
    skip_wip: bool = True

    # Alert mode — active only when operator-level ``alerts.enabled`` is on.
    alerts: ProjectAlertsConfig = Field(default_factory=ProjectAlertsConfig)

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

    # v1.75 rejection predictor — opt-in. When enabled, drafts are scored
    # with the per-project sklearn model (training it automatically once
    # enough operator actions accumulate) and high-P(rejection) findings are
    # auto-suppressed. Off by default: v1.5+ paths activate only via
    # explicit config.
    rejection_predictor_enabled: bool = False

    # v2 features
    fine_tuned_model: FineTunedModelConfig = Field(default_factory=FineTunedModelConfig)
    merge_queue: MergeQueueConfig = Field(default_factory=MergeQueueConfig)
    # Shepherding pass over the operator's own PRs (draft replies to
    # reviewers, rebase/staleness alerts). v2 — opt-in per project.
    shepherding_enabled: bool = False

    # LLM sub-checks (v1) — e.g. ["coverage"]
    llm_checks: list[str] = Field(default_factory=list)
    api_misuse: APIMisuseConfig = Field(default_factory=APIMisuseConfig)
    backport: BackportConfig = Field(default_factory=BackportConfig)

    # Full-file + first-party-import context for review prompts (v1).
    context: ContextConfig = Field(default_factory=ContextConfig)

    # Differential test runner (§9). Disabled by default; see docs/test-runner.md.
    tests: TestExecutionConfig = Field(default_factory=TestExecutionConfig)

    @field_validator("llm_checks")
    @classmethod
    def llm_checks_warn_unknown(cls, v: list[str]) -> list[str]:
        known = {
            "api-misuse",
            "backport",
            "coverage",
            "issue-link",
            "malicious-prompt",
            "pr-description",
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

    @field_validator("auto_review_policy")
    @classmethod
    def auto_review_policy_valid(cls, v: str) -> str:
        v = v.strip().lower()
        known = {"all", "mentioned_or_authored", "none"}
        if v not in known:
            msg = f"auto_review_policy must be one of {sorted(known)}, got {v!r}"
            raise ValueError(msg)
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
