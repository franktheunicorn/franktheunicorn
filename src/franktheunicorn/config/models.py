"""Pydantic models for operator and per-project YAML configuration."""

from __future__ import annotations

import logging
import re
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
#: How a remote command is handed to ``ssh_command``. See
#: ``RemoteExecutionConfig.command_mode``.
_KNOWN_COMMAND_MODES: frozenset[str] = frozenset({"auto", "argv", "stdin"})

#: Model for the claude agent-CLI reviewer. Every PR gets a full-diff pass whose
#: output is parsed, not read, then filtered through anti-patterns and dedup —
#: not work for the reasoning tier. An alias, not a pinned ID, so it doesn't rot.
CLAUDE_CLI_DEFAULT_MODEL = "sonnet"


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
    # How a command reaches the far side.
    #
    # ``ssh host 'cmd'`` takes it as a trailing argument, and that was assumed
    # unconditionally. Some wrappers use that positional slot for something else
    # entirely: ``sf workspace ssh 'cd /x && claude …'`` answers "Workspace not
    # found: cd /x && claude …" — it wants a *workspace name* there and has no
    # remote-command form at all. Others simply ignore extra arguments and open an
    # interactive shell, which is worse, because the session then exits on EOF
    # with status 0 and empty output and every caller downstream reads that as
    # "the repo has no diff" — a silent clean review.
    #
    # ``stdin`` drives such a wrapper by piping the command into the shell it
    # opens. ``auto`` (the default) settles it by experiment once per config:
    # round-trip a sentinel through each shape and keep whichever comes back.
    # Nothing is inferred from the wrapper's name.
    command_mode: str = "auto"
    remote_workspace_dir: str = "~/.frank-remote"
    clone_url_template: str = "https://github.com/{owner}/{repo}.git"
    prepare_timeout_seconds: int = 600

    #: Settled delivery shape, filled in by the executor's first probe. Cached on
    #: the config rather than the executor because ``make_executor`` builds a fresh
    #: executor per call — without this the probe would run on every command.
    _resolved_command_mode: str | None = PrivateAttr(default=None)

    @field_validator("mode")
    @classmethod
    def mode_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in _KNOWN_REMOTE_MODES:
            msg = f"remote.mode must be one of {sorted(_KNOWN_REMOTE_MODES)}, got {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("command_mode")
    @classmethod
    def command_mode_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in _KNOWN_COMMAND_MODES:
            msg = f"remote.command_mode must be one of {sorted(_KNOWN_COMMAND_MODES)}, got {v!r}"
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
    # Matches the registry default; see CLAUDE_CLI_DEFAULT_MODEL.
    model: str = CLAUDE_CLI_DEFAULT_MODEL
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

    @model_validator(mode="after")
    def default_claude_model(self) -> AgentCLIReviewerConfig:
        """Default the claude entry's model wherever the entry was built.

        Here, not in the seed: ``assemble_agent_cli_registry`` merges by name and
        never by field, so an operator overriding one field replaces the seed
        outright and would drop back to the CLI's default. ``model_fields_set``
        keeps an explicit ``model: ""`` meaning "pass no flag".
        """
        if self.name == "claude" and "model" not in self.model_fields_set:
            self.model = CLAUDE_CLI_DEFAULT_MODEL
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
    """Seed the registry with the auto-detected agent reviewers.

    Each is named after its binary and defaults to ``enabled="auto"``, so it runs
    only when that binary is present on PATH (local mode). Operators can override any entry by name
    in ``operator.yaml`` or add their own agents to the list.
    """
    return [
        # model comes from default_claude_model, not from here.
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
        # Cursor's headless agent. The binary is ``cursor-agent``, not ``cursor``.
        #
        # ``--mode ask`` is not a preference, it is the safety property: ``-p`` on
        # its own is documented as having "access to all tools, including write and
        # shell", and this is a reviewer pointed at a checkout of someone else's
        # PR. ``ask`` is the read-only Q&A mode, which is all a reviewer needs.
        #
        # The argv shape works out the same as claude's even though the two differ:
        # for claude ``-p`` carries the prompt, for cursor-agent ``-p``/``--print``
        # is a boolean and the prompt is positional. Either way the suffix is
        # ``[-p, <prompt>]``. Verified against ``cursor-agent --help`` and by
        # invoking it (it parsed the arguments and stopped at authentication).
        # Named after the binary, like every other seed, because ``cli_path``
        # defaults to ``name``: a config entry of ``- name: cursor`` would
        # otherwise look for a binary called ``cursor`` (the editor) and quietly
        # never run.
        AgentCLIReviewerConfig(
            name="cursor-agent",
            prompt_mode="flag",
            prompt_arg="-p",
            extra_args=["--mode", "ask"],
        ),
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
    {"stub", "claude", "claude-code", "openai", "gemini", "ollama", "llama-cpp", "vllm", "rlm"}
)

# Transports for the "claude-code" provider (see LLMBackendConfig.transport).
KNOWN_LLM_TRANSPORTS: frozenset[str] = frozenset({"cli", "acp"})

# Combine strategies for the optional RLM rejection judge (v1.5).
KNOWN_RLM_COMBINE_MODES: frozenset[str] = frozenset({"max", "average", "rlm-only"})

# RLM execution backends: deterministic in-process map-reduce, or the
# authentic "model writes code" notebook running in a sandboxed container.
KNOWN_RLM_EXECUTION_MODES: frozenset[str] = frozenset({"map-reduce", "notebook"})


class LLMBackendConfig(BaseModel):
    """Config for which LLM backend to use for review generation."""

    provider: str = "stub"
    model: str = ""
    api_key_env: str = ""
    base_url: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    # Only consulted when ``provider == "claude-code"``: talks to a local
    # Claude Code agent instead of calling the Anthropic API, so no API key
    # is needed (billed via the agent's own logged-in auth). ``transport``
    # picks how: "cli" (default) shells out to ``cli_path`` in headless
    # prompt mode (``claude -p ... --output-format json``); "acp" speaks
    # the Agent Client Protocol (JSON-RPC over stdio,
    # https://agentclientprotocol.com/) to ``acp_command`` -- empty defaults
    # to ``npx @zed-industries/claude-code-acp``. ``cli_timeout_seconds``
    # bounds a single call under either transport.
    transport: str = "cli"
    cli_path: str = "claude"
    acp_command: str = ""
    cli_timeout_seconds: int = 300
    # Recursive Language Model settings (v1.5). Only consulted when
    # ``provider == "rlm"``; ignored (with a warning) for other providers.
    rlm: RLMConfig | None = None

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

    @field_validator("transport")
    @classmethod
    def transport_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_LLM_TRANSPORTS:
            logger.warning(
                "Unknown claude-code transport '%s'; known values: %s",
                v,
                ", ".join(sorted(KNOWN_LLM_TRANSPORTS)),
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

    @model_validator(mode="after")
    def warn_rlm_misplaced(self) -> LLMBackendConfig:
        if self.rlm is not None and self.provider != "rlm":
            logger.warning(
                "'rlm' settings provided on a '%s' backend; they are only used when "
                "provider == 'rlm' and will be ignored here.",
                self.provider,
            )
        return self


def _positive(v: int, name: str) -> int:
    if v <= 0:
        msg = f"{name} must be positive"
        raise ValueError(msg)
    return v


class RLMConfig(BaseModel):
    """Recursive Language Model orchestration settings (v1.5, opt-in).

    The RLM decomposes a large PR (per file, then per hunk) and dispatches
    focused "leaf" reviews to an ordinary backend (``leaf``), then aggregates
    the findings. Recursion is bounded entirely by these knobs — cost never
    runs away. When the whole PR fits under ``leaf_token_budget`` the engine
    skips decomposition and behaves like a single normal backend call.
    """

    leaf: LLMBackendConfig = Field(default_factory=LLMBackendConfig)
    max_depth: int = 2
    max_sub_calls: int = 30
    leaf_token_budget: int = 8000
    total_token_budget: int = 200_000
    concurrency: int = 4
    # When True, spend one extra leaf call synthesizing an overall vibe;
    # otherwise the vibe is assembled deterministically from leaf vibes.
    synthesis_call: bool = False

    # Execution backend. "map-reduce" (default) runs the deterministic
    # in-process decomposition. "notebook" runs the authentic RLM: the model
    # writes Python in a Jupyter notebook executed inside a sandboxed
    # container, with the PR bound to a `CONTEXT` variable and helpers to
    # recurse into any model and search the code. Notebook mode is worker-only
    # (needs Docker); it degrades to map-reduce when Docker/Jupyter are absent.
    execution: str = "map-reduce"
    # Container image for notebook mode. Must have nbclient + ipykernel
    # available (e.g. a prebuilt jupyter image). Run with --network=none.
    image: str = "python:3.12-slim"
    container_timeout: int = 300
    # Budget on brokered model calls per notebook session (cost guard).
    max_model_calls: int = 40

    @field_validator("max_depth")
    @classmethod
    def depth_in_range(cls, v: int) -> int:
        if v < 1:
            msg = "max_depth must be >= 1"
            raise ValueError(msg)
        return v

    @field_validator(
        "max_sub_calls",
        "leaf_token_budget",
        "total_token_budget",
        "concurrency",
        "container_timeout",
        "max_model_calls",
    )
    @classmethod
    def caps_positive(cls, v: int) -> int:
        return _positive(v, "RLM budget value")

    @field_validator("execution")
    @classmethod
    def execution_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_RLM_EXECUTION_MODES:
            msg = f"execution must be one of: {', '.join(sorted(KNOWN_RLM_EXECUTION_MODES))}"
            raise ValueError(msg)
        return v


class RLMScoringConfig(BaseModel):
    """Optional RLM-based scoring/rejection judges (v1.5, opt-in, default off).

    These are *additive* — they never replace the sklearn rejection predictor.
    ``interest_enabled`` lets the RLM produce the ``llm_interest`` scoring
    signal; ``rejection_judge_enabled`` lets it contribute a P(rejection)
    estimate that is combined with the sklearn value per ``rejection_combine``.
    """

    interest_enabled: bool = False
    rejection_judge_enabled: bool = False
    rejection_combine: str = "max"
    leaf: LLMBackendConfig = Field(default_factory=LLMBackendConfig)
    max_sub_calls: int = 20
    leaf_token_budget: int = 8000
    total_token_budget: int = 120_000

    @field_validator("rejection_combine")
    @classmethod
    def combine_must_be_known(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in KNOWN_RLM_COMBINE_MODES:
            msg = f"rejection_combine must be one of: {', '.join(sorted(KNOWN_RLM_COMBINE_MODES))}"
            raise ValueError(msg)
        return v

    @field_validator("max_sub_calls", "leaf_token_budget", "total_token_budget")
    @classmethod
    def caps_positive(cls, v: int) -> int:
        return _positive(v, "RLM scoring budget value")


# LLMBackendConfig references RLMConfig (forward ref) which references
# LLMBackendConfig — resolve the cycle now that both are defined.
LLMBackendConfig.model_rebuild()


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


class SecurityVerifierConfig(BaseModel):
    """Config for the deep verifier: does this reported vulnerability exist?

    Triage reads the *report* and rules on plausibility from the text plus a CVE
    lookup. This reads the *code*: it puts a coding agent in a checkout of the
    project with the report in hand and asks it to go and look. Slow and
    expensive — one long agent run per branch — so it is off by default and never
    fires without either a button press or an explicit opt-in at import.

    ``reviewer`` names an entry in ``agent_cli_reviewers`` and borrows its CLI
    path, argv shape and — the point of this, for a remote setup — its
    ``remote`` block. There is no second copy of "how do I reach the box where
    the agent runs".

    ``extra_args`` is where the depth knobs go. Deliberately not a hardcoded
    ``--ultra``: the flag for "think as hard as you can" differs per CLI and
    changes between releases, so the operator names it and it stays correct
    without a code change.

    The branch list is the reason this is per-project rather than per-file: a
    vulnerability that is real on ``master`` may be absent from ``branch-3.5``
    because the code was rewritten, or real on both and only fixed on one. A
    verdict without a branch attached isn't actionable.
    """

    enabled: bool = False
    #: Which ``agent_cli_reviewers`` entry to borrow the CLI and remote config from.
    reviewer: str = "claude"
    #: Override the reviewer's model. Empty means "whatever the reviewer uses".
    model: str = ""
    #: Appended to the agent's argv. Where "run it in ultra mode" is expressed.
    extra_args: list[str] = Field(default_factory=list)
    #: Per-branch budget. A real investigation reads files and runs greps; the
    #: review-path default of 300s is nowhere near enough.
    timeout_seconds: int = 1800
    #: Branches beyond the default one, newest-committed first. Each costs a full
    #: agent run, so the cap is the cost control.
    max_branches: int = 3
    #: A branch with no commits in this long is not a branch anyone is shipping
    #: from, and verifying against it is spend with no consumer.
    branch_active_within_days: int = 180
    #: Regexes for "a named version branch". The defaults cover Spark's
    #: ``branch-4.0``/``branch-3.5``, plus the common ``release-*`` and ``v1.2``
    #: shapes. Anchored at the start; matched against the short branch name.
    branch_patterns: list[str] = Field(
        default_factory=lambda: [r"^branch-\d", r"^release[-/]", r"^v?\d+\.\d+$", r"^stable[-/]"]
    )

    @field_validator("branch_patterns")
    @classmethod
    def patterns_must_compile(cls, v: list[str]) -> list[str]:
        """Reject an unparseable regex here, where the operator can see it.

        Otherwise one typo escaped as a bare ``re.error`` out of
        ``select_branches`` — a function documented "never raises" — killing the
        WorkerCommand with a message about character sets and none of the
        name-the-setting diagnostics every other gate in that module produces.
        """
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                msg = f"branch_patterns entry {pattern!r} is not a valid regex: {exc}"
                raise ValueError(msg) from exc
        return v

    #: How much of the report to hand the agent. A scanner archive's raw entry can
    #: be enormous and the prompt has to leave room for the agent to work.
    max_report_chars: int = 12_000
    #: Refuse to verify a report whose text trips the prompt-injection patterns,
    #: rather than running it and saying so.
    #:
    #: Off by default, because blocking assumes an intake this tool doesn't have.
    #: On an ASF project a report reaches the operator through security@, gets
    #: read by the security team, and gets read again by the maintainer before it
    #: is pasted in — three humans ahead of the agent. Against that, a hard
    #: refusal buys little and costs a specific, likely case: a report *about* a
    #: prompt-injection vulnerability quotes the payload it is reporting, so the
    #: detector fires on precisely the reports an ML project most needs to
    #: verify, and the feature refuses the ones that matter.
    #:
    #: Turn it on if reports reach the verifier without a human in between — an
    #: unattended email ingest, or a bulk import of somebody else's scanner
    #: output. The patterns are still scanned and still reported either way; this
    #: only decides whether a hit stops the run.
    refuse_on_injection: bool = False

    #: Where the verification checkout lives, under the remote workspace dir. Kept
    #: separate from the review pipeline's clone on purpose: this one gets checked
    #: out onto arbitrary release branches and left there, and doing that to the
    #: tree the review path is mid-diff on would corrupt an unrelated review.
    workspace_subdir: str = "security-verify"


class SecurityTriageConfig(BaseModel):
    """Config for security report triage feature."""

    enabled: bool = False
    email: SecurityEmailConfig = Field(default_factory=SecurityEmailConfig)
    nvd_api_key_env: str = ""  # optional, for higher NVD rate limits
    auto_triage: bool = True  # automatically run LLM triage on new reports
    sandbox_enabled: bool = False  # allow sandbox POC execution
    verifier: SecurityVerifierConfig = Field(default_factory=SecurityVerifierConfig)


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
                continue
            # Field-level merge, not just entry-level. operator.yaml says "Entries
            # merge by name with the built-in defaults, so you can override just
            # one" — and this only ever appended *missing names*, so
            # `- name: codex` plus one field replaced the seed outright and lost
            # prompt_mode="subcommand": the reviewer was invoked as `codex -p
            # <prompt>` instead of `codex exec <prompt>` and produced nothing.
            #
            # model_fields_set is what makes this safe: only fields the operator
            # didn't write are filled, so an explicit `model: ""` still means
            # "pass no flag".
            supplied = by_name[seed.name]
            for field_name in type(seed).model_fields:
                if field_name == "name" or field_name in supplied.model_fields_set:
                    continue
                setattr(supplied, field_name, getattr(seed, field_name))

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
    # Prose description of the project's threat model / trust boundaries, fed
    # into security-report triage. Many "vulnerabilities" are really the
    # project's documented stance (e.g. Spark treats submitted code, models,
    # and pipelines as trusted and will run arbitrary code from them). Stating
    # that here lets triage mark such reports as expected behavior instead of
    # flagging them as findings. Empty by default (triage falls back to
    # README/SECURITY.md context only).
    security_model: str = ""
    # Repo-relative path to a threat-model document to load as the security
    # model when ``security_model`` above is not set inline. Empty means
    # auto-discover a conventional threat-model file (e.g.
    # ``.frank/security-model.md``, ``THREAT_MODEL.md``) if one is present in
    # the checked-out repo. SECURITY.md is intentionally not used here — by
    # convention it is a vulnerability-reporting policy, not a trust-boundary
    # statement.
    security_model_file: str = ""
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
    # Poll cost control. A PR whose upstream ``updated_at`` hasn't moved since
    # the last poll — and whose listed title/labels/reviewers/assignees/draft
    # state are unchanged — is skipped: no files/detail/comment fetches, no
    # blame git fetches, no downstream review work. Every refreshed PR costs at
    # least three API calls (files, detail, comments), so apache/spark's ~450
    # open PRs came to well over a thousand calls per cycle against a 5000/hour
    # budget. The limit ran dry mid-cycle, ingestion degraded to HTML scrapes,
    # and new PRs stopped landing at all.
    #
    # Some scoring signals age on their own (staleness, "waiting on author"),
    # so an untouched PR is still fully re-processed once this many hours have
    # passed. 0 disables the skip and re-processes everything every cycle.
    poll_refresh_hours: int = 24
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
    # Noise suppression. Test scaffolding (main guards, license headers, the
    # same six-line harness call in every test file) is duplicated on purpose
    # and telling the author about it is pure noise. A duplicated block that
    # already lives in more than this many *other* files in the repo is an
    # established idiom, not a copy-paste — stay quiet. Set to 0 to disable
    # the ubiquity check and report every match.
    copypasta_max_repo_occurrences: int = 2
    # Extra per-project regexes (matched against whitespace-collapsed lines)
    # that count as boilerplate on top of the built-in set. A block is only
    # reported if it still has copypasta_min_lines of non-boilerplate left.
    copypasta_ignore_patterns: list[str] = Field(default_factory=list)

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

    # Recursive Language Model scoring/rejection judges (v1.5, opt-in).
    rlm_scoring: RLMScoringConfig = Field(default_factory=RLMScoringConfig)

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

    @field_validator("poll_refresh_hours")
    @classmethod
    def poll_refresh_hours_valid(cls, v: int) -> int:
        if v < 0:
            msg = "poll_refresh_hours must be >= 0 (0 re-processes every PR every cycle)"
            raise ValueError(msg)
        return v

    @field_validator("copypasta_min_lines")
    @classmethod
    def copypasta_min_lines_valid(cls, v: int) -> int:
        if v < 2:
            msg = "copypasta_min_lines must be at least 2"
            raise ValueError(msg)
        return v

    @field_validator("copypasta_scan_extensions")
    @classmethod
    def copypasta_scan_extensions_valid(cls, v: list[str]) -> list[str]:
        # An empty list reads like "scan everything" and silently means the
        # opposite: no chunk matches any extension, so detection is off while
        # copypasta_enabled still says it's on.
        if not v:
            msg = (
                "copypasta_scan_extensions must list at least one extension "
                "(set copypasta_enabled: false to turn the check off)"
            )
            raise ValueError(msg)
        return v

    @field_validator("copypasta_max_repo_occurrences")
    @classmethod
    def copypasta_max_repo_occurrences_valid(cls, v: int) -> int:
        if v < 0:
            msg = "copypasta_max_repo_occurrences must be >= 0 (0 disables the check)"
            raise ValueError(msg)
        return v

    @field_validator("copypasta_ignore_patterns")
    @classmethod
    def copypasta_ignore_patterns_valid(cls, v: list[str]) -> list[str]:
        # Fail at config load, not halfway through a review cycle.
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                msg = f"copypasta_ignore_patterns entry {pattern!r} is not a valid regex: {exc}"
                raise ValueError(msg) from exc
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
