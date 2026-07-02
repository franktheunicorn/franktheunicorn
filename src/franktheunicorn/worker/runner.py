"""
Worker runner — polls each configured forge on an interval and processes PRs.

This is the main loop for the worker service. It:
1. Loads operator and project configs from YAML
2. Builds a ForgeClient per forge entry referenced by configured projects
3. Polls each project against its forge for PRs
4. Scores and stores results
5. Runs the stub review drafter on new/updated PRs
6. Sleeps and repeats
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any

import django

if TYPE_CHECKING:
    import httpx

    from franktheunicorn.config.models import (
        ClaudeCLIConfig,
        CodeRabbitConfig,
        OperatorConfig,
        ProjectConfig,
        SnowflakeReviewConfig,
    )
    from franktheunicorn.core.models import PullRequest
    from franktheunicorn.data_access.github.diff_fetcher import DiffFetcher


def _raise_keyboard_interrupt(signum: int, frame: FrameType | None) -> None:
    """Convert SIGTERM into KeyboardInterrupt so the main loop's ``finally``
    cleanup runs (close clients, release worker.lock). Container orchestrators
    send SIGTERM before SIGKILL on shutdown.
    """
    raise KeyboardInterrupt


logger = logging.getLogger(__name__)

_VALID_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET")

# Sentinel returned by _resolve_cwd_for_tool as temp_branch when running
# remotely over SSH.  Distinguishes "no branch needed" (remote) from "merge
# conflict" (None) so callers don't misidentify remote execution as a conflict.
# The value can never collide with a real git branch name because the only
# branch name franktheunicorn creates is "franktheunicorn-review-{pr.number}".
_REMOTE = "<<remote>>"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="franktheunicorn-worker",
        description="Run the franktheunicorn background worker.",
    )
    parser.add_argument(
        "--log-level",
        choices=_VALID_LOG_LEVELS,
        default=None,
        help=(
            "Set the log level (default: from operator.yaml's log_level, or INFO). "
            "Overrides both YAML config and the FRANK_LOG_LEVEL env var."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_const",
        const="DEBUG",
        dest="log_level",
        help="Shortcut for --log-level=DEBUG.",
    )
    return parser.parse_args(argv)


def run_worker(argv: Sequence[str] | None = None) -> None:
    """Main worker entry point.

    ``argv`` is parsed for ``--log-level`` / ``--debug``. Default ``None``
    means "no CLI args" — argparse is *not* allowed to fall back to
    ``sys.argv`` here, since this function is also called from the Django
    ``run_worker`` management command (where ``sys.argv`` contains
    ``manage.py``-specific tokens that this parser would reject). The
    ``__main__`` entry point below is the only caller that forwards
    ``sys.argv[1:]`` explicitly.
    """
    args = _parse_args(list(argv) if argv is not None else [])

    from franktheunicorn.env_loader import load_project_dotenv

    load_project_dotenv()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "franktheunicorn.settings")
    django.setup()

    from django.conf import settings

    from franktheunicorn.backends import make_client
    from franktheunicorn.backends.base import ForgeClient
    from franktheunicorn.backends.mock import MockForgeClient
    from franktheunicorn.config.loader import load_operator_config, load_project_configs
    from franktheunicorn.config.resolver import ensure_github_username, get_forge_entry

    # Precedence: --log-level CLI flag > FRANK_LOG_LEVEL env > operator.yaml > INFO.
    # The env var path is already applied inside the resolver, so reading
    # settings.FRANK_LOG_LEVEL covers env + YAML.
    log_level = args.log_level or getattr(settings, "FRANK_LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.debug("Log level set to %s", log_level)

    # Acquire instance lock to prevent duplicate workers.
    data_dir = Path(getattr(settings, "DATA_DIR", Path.home() / ".review-agent"))
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / "worker.lock"
    lock_fd = open(lock_path, "w")  # noqa: SIM115
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error("Another worker instance is already running (lock: %s). Exiting.", lock_path)
        lock_fd.close()
        sys.exit(1)

    # The flock guarantees no other worker holds these rows, so anything
    # still "running" was orphaned by a crash/kill — requeue it (the worker
    # must be safe to kill and restart).
    from franktheunicorn.worker.commands import requeue_interrupted_commands

    try:
        requeue_interrupted_commands()
    except Exception:
        logger.exception("Failed to requeue interrupted worker commands")

    operator_config = load_operator_config(settings.FRANK_OPERATOR_CONFIG)
    # Infer github_username from the token if not explicitly set. We do this
    # here rather than during Django settings load so settings has no live
    # network dependency (manage.py check / migrate / tests stay offline).
    ensure_github_username(operator_config)
    project_configs = load_project_configs(settings.FRANK_PROJECTS_DIR)

    if not project_configs:
        logger.warning("No project configs found in %s", settings.FRANK_PROJECTS_DIR)
        logger.info("The worker will keep running and check again each cycle.")

    # Build one ForgeClient per forge entry referenced by configured
    # projects. Projects sharing a forge share a client. In mock mode a
    # single MockForgeClient serves all projects regardless of their
    # ``forge:`` field.
    clients: dict[str, ForgeClient] = {}
    if settings.FRANK_MOCK_MODE:
        logger.info("Running in MOCK mode — using fixture data")
        mock_client: ForgeClient = MockForgeClient(settings.FRANK_FIXTURES_DIR)
        # Same instance used for every project's forge name.
        for pc in project_configs:
            from franktheunicorn.config.models import ProjectConfig

            if isinstance(pc, ProjectConfig):
                clients[pc.forge] = mock_client
        if not clients:
            clients["github"] = mock_client
    else:
        from franktheunicorn.config.models import ProjectConfig

        forge_names_used = {
            pc.forge for pc in project_configs if isinstance(pc, ProjectConfig) and pc.enabled
        }
        for forge_name in sorted(forge_names_used):
            try:
                entry = get_forge_entry(operator_config, forge_name)
            except KeyError as exc:
                logger.error(
                    "Skipping forge %r: %s. Projects using this forge will not be polled.",
                    forge_name,
                    exc,
                )
                continue
            try:
                clients[forge_name] = make_client(entry)
            except (NotImplementedError, ValueError) as exc:
                logger.error(
                    "Could not build client for forge %r (type=%s): %s",
                    forge_name,
                    entry.type,
                    exc,
                )
                continue
            logger.info(
                "Forge %r ready (type=%s, base_url=%s)",
                forge_name,
                entry.type,
                entry.base_url,
            )
        if not clients:
            logger.error(
                "No usable forge clients. Configure operator.yaml::forges or set "
                "FRANK_MOCK_MODE=true. Exiting."
            )
            sys.exit(1)

    poll_interval = settings.FRANK_POLL_INTERVAL
    logger.info(
        "Worker starting as %s. Poll interval: %ds",
        operator_config.github_username or "(username not set)",
        poll_interval,
    )

    disabled_backends = _check_backends(operator_config)
    _check_ssh_configs(operator_config)

    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    try:
        while True:
            # A cycle failure (e.g. transient "database is locked" from the
            # shared SQLite file) must not kill the daemon — log and retry on
            # the next poll.
            try:
                _run_cycle(
                    clients,
                    project_configs,
                    operator_config.github_username,
                    operator_config,
                    disabled_backends,
                )
            except Exception:
                logger.exception("Poll cycle failed; retrying next interval.")
            # Sleep until the next poll, but wake every COMMAND_POLL_INTERVAL
            # to drain any WorkerCommand rows the dashboard queued (manual
            # test runs, force-run agents, security sandbox). This keeps
            # web-triggered actions responsive even when poll_interval is
            # several minutes.
            from franktheunicorn.worker.commands import process_pending_commands

            command_poll_interval = 5
            elapsed = 0
            while elapsed < poll_interval:
                try:
                    processed = process_pending_commands(operator_config)
                    if processed:
                        logger.info("Drained %d worker command(s)", processed)
                except Exception:
                    logger.exception("Error draining worker commands")
                wait = min(command_poll_interval, poll_interval - elapsed)
                time.sleep(wait)
                elapsed += wait
            logger.info("Next poll cycle...")
    except KeyboardInterrupt:
        logger.info("Worker shutting down.")
    finally:
        # Close each unique client exactly once (mock mode reuses one instance).
        for c in {id(v): v for v in clients.values()}.values():
            try:
                c.close()
            except Exception:
                logger.debug("Error closing forge client", exc_info=True)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _mask_key(key: str) -> str:
    """Return a masked version of an API key for safe logging: first 2 + last 2 chars."""
    if len(key) <= 4:
        return "*" * len(key)
    return key[:2] + "…" + key[-2:]


def _seed_token_param_fallback(model: str, base_url: str, token_param: str) -> None:
    """Persist the discovered token param name so OpenAIBackend skips its own first-attempt error."""
    try:
        from franktheunicorn.core.models import LLMBackendFallback

        LLMBackendFallback.objects.update_or_create(
            provider="openai",
            model=model,
            base_url=base_url,
            defaults={"token_param": token_param},
        )
    except Exception:
        logger.debug("Could not seed LLM token-param fallback state to DB.", exc_info=True)


def _openai_chat_preflight(
    openai: object,
    client_kwargs: dict[str, str],
    model: str,
    base_url: str,
    idx: int,
    masked: str,
    disabled: set[int],
) -> None:
    """Verify an OpenAI-compatible endpoint that doesn't support /models via a minimal chat call.

    Tries ``max_tokens`` first; if the server rejects it with a deprecation error (e.g. Snowflake
    Cortex requires ``max_completion_tokens``), retries once with the alternative param name.
    On success after retry, seeds ``LLMBackendFallback`` so ``OpenAIBackend`` starts with the
    right param name and avoids its own first-attempt failure.

    Mutates ``disabled`` on failure. Logs OK or WARNING.
    """
    import openai as _openai

    client = _openai.OpenAI(**client_kwargs)  # type: ignore[arg-type]
    token_param = "max_tokens"

    for attempt in range(2):
        try:
            client.chat.completions.create(  # type: ignore[call-overload]
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                **{token_param: 1},
            )
        except _openai.BadRequestError as exc:
            msg = str(exc).lower()
            if attempt == 0 and ("max_tokens" in msg or "max_completion_tokens" in msg):
                token_param = "max_completion_tokens"
                continue
            logger.warning(
                "Backend[%d] openai (%s key=%s): preflight chat check failed — %s — "
                "backend disabled for this run",
                idx,
                base_url,
                masked,
                exc,
            )
            disabled.add(idx)
            return
        except Exception as exc:
            logger.warning(
                "Backend[%d] openai (%s key=%s): preflight chat check failed — %s — "
                "backend disabled for this run",
                idx,
                base_url,
                masked,
                exc,
            )
            disabled.add(idx)
            return
        else:
            if token_param != "max_tokens":
                _seed_token_param_fallback(model, base_url, token_param)
            logger.info(
                "Backend[%d] openai (%s key=%s): OK (no /models endpoint; chat check passed)",
                idx,
                base_url,
                masked,
            )
            return


def _check_backends(operator_config: OperatorConfig) -> frozenset[int]:
    """Probe each configured LLM backend and return indices of those that fail.

    Logs provider, URL, and masked API key for every backend checked.
    Backends that pass are logged at INFO; failures at WARNING.
    Skips providers that need no API key (stub, ollama).
    """
    import os

    disabled: set[int] = set()

    for idx, bc in enumerate(operator_config.llm_backends):
        provider = bc.provider.lower()
        if provider in ("stub", "ollama"):
            continue

        key_env = bc.api_key_env or {
            "claude": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "gemini": "GOOGLE_API_KEY",
        }.get(provider, "")
        api_key = os.environ.get(key_env, "") if key_env else ""

        base_url = bc.base_url or {
            "claude": "https://api.anthropic.com",
            "openai": "https://api.openai.com",
            "gemini": "https://generativelanguage.googleapis.com",
        }.get(provider, "(default)")

        if not api_key:
            logger.warning(
                "Backend[%d] %s (%s): no API key in env var %r — backend disabled for this run",
                idx,
                provider,
                base_url,
                key_env or "(unset)",
            )
            disabled.add(idx)
            continue

        masked = _mask_key(api_key)
        try:
            if provider == "claude":
                import anthropic

                anthropic.Anthropic(api_key=api_key).models.list()
            elif provider == "openai":
                import openai

                kwargs: dict[str, str] = {"api_key": api_key}
                if bc.base_url:
                    kwargs["base_url"] = bc.base_url
                try:
                    openai.OpenAI(**kwargs).models.list()  # type: ignore[arg-type]
                except openai.NotFoundError:
                    # Some OpenAI-compatible endpoints (e.g. Snowflake Cortex)
                    # don't implement /models. Fall back to a minimal chat call.
                    _openai_chat_preflight(
                        openai, kwargs, bc.model or "gpt-4o", base_url, idx, masked, disabled
                    )
                    continue
            elif provider == "gemini":
                from google import genai

                genai.Client(api_key=api_key).models.list()
            else:
                logger.debug(
                    "Backend[%d] %s (%s key=%s): no preflight check for this provider",
                    idx,
                    provider,
                    base_url,
                    masked,
                )
                continue
        except Exception as exc:
            logger.warning(
                "Backend[%d] %s (%s key=%s): preflight check failed — %s — "
                "backend disabled for this run",
                idx,
                provider,
                base_url,
                masked,
                exc,
            )
            disabled.add(idx)
            continue

        logger.info(
            "Backend[%d] %s (%s key=%s): OK",
            idx,
            provider,
            base_url,
            masked,
        )

    return frozenset(disabled)


def _check_ssh_configs(operator_config: OperatorConfig) -> frozenset[str]:
    """Probe SSH connectivity for each enabled tool that uses remote SSH execution.

    Logs results at INFO (reachable) or WARNING (unreachable) so operators see
    SSH problems at startup rather than buried in per-PR retry logs.
    Returns the names of tools whose SSH probe failed.
    """
    from franktheunicorn.review.tool_executor import RemoteSSHExecutor

    failed: set[str] = set()

    tool_remotes: list[tuple[str, Any]] = [
        ("coderabbit", operator_config.coderabbit),
        ("claude_cli", operator_config.claude_cli),
        ("snowflake_review", operator_config.snowflake_review),
    ]
    for tool_name, tool_config in tool_remotes:
        if not tool_config.enabled:
            continue
        remote = tool_config.remote
        if remote.mode != "ssh":
            continue

        executor = RemoteSSHExecutor(config=remote)
        ssh_display = " ".join(remote.ssh_command)
        target = executor._ssh_target()
        target_hint = f" (target={target!r})" if target else " (no host — wrapper handles routing)"

        if executor._probe_ssh():
            logger.info("SSH[%s] %s%s: OK", tool_name, ssh_display, target_hint)
        else:
            logger.warning(
                "SSH[%s] %s%s: preflight probe failed"
                " — SSH may be misconfigured; remote git operations will retry but are"
                " unlikely to succeed until connectivity is restored",
                tool_name,
                ssh_display,
                target_hint,
            )
            failed.add(tool_name)

    return frozenset(failed)


_HEALTH_STALE_DAYS = 7


def _maybe_refresh_repo_health(
    pc: object,
    repo_path: Path | None,
) -> None:
    """Run repo health analysis if the snapshot is missing or stale (>7 days)."""
    if repo_path is None:
        return
    try:
        from datetime import timedelta

        from django.utils import timezone

        from franktheunicorn.config.models import ProjectConfig
        from franktheunicorn.core.models import Project
        from franktheunicorn.worker.repo_health import analyze_repo_health, snapshot_to_dict

        if not isinstance(pc, ProjectConfig):
            return

        project = Project.objects.filter(owner=pc.owner, repo=pc.repo).first()
        if project is None:
            return

        cutoff = timezone.now() - timedelta(days=_HEALTH_STALE_DAYS)
        if project.repo_health_analyzed_at and project.repo_health_analyzed_at >= cutoff:
            return

        logger.info("Running repo health analysis for %s/%s ...", pc.owner, pc.repo)
        snapshot = analyze_repo_health(repo_path)
        project.repo_health_snapshot = snapshot_to_dict(snapshot)
        project.repo_health_analyzed_at = timezone.now()
        project.save(update_fields=["repo_health_snapshot", "repo_health_analyzed_at"])
        logger.info(
            "Repo health analysis complete for %s/%s: %d churn files, %d contributors",
            pc.owner,
            pc.repo,
            len(snapshot.high_churn_files),
            len(snapshot.contributors),
        )
    except Exception:
        logger.debug(
            "Repo health analysis failed for %s/%s",
            getattr(pc, "owner", "?"),
            getattr(pc, "repo", "?"),
            exc_info=True,
        )


def _build_repo_health_context(pr: object) -> str:
    """Format repo health context for a PR's changed files."""
    try:
        from franktheunicorn.worker.repo_health import format_health_for_review, snapshot_from_dict

        snapshot_data = pr.project.repo_health_snapshot  # type: ignore[attr-defined]
        if not snapshot_data:
            return ""
        snapshot = snapshot_from_dict(snapshot_data)
        changed_files: list[str] = pr.changed_files or []  # type: ignore[attr-defined]
        return format_health_for_review(snapshot, changed_files)
    except Exception:
        return ""


def process_pr(
    pr: PullRequest,
    pc: ProjectConfig,
    operator_config: OperatorConfig | None,
    disabled_backends: frozenset[int] = frozenset(),
    diff_http: httpx.Client | None = None,
    repo_path: Path | None = None,
    *,
    force: bool = False,
    log_lines: list[str] | None = None,
) -> list[Any]:
    """Run the full review pipeline for a single PR.

    This is the canonical per-PR processing path shared by the worker poll
    loop, the backfill pass, and the dashboard "Force Run Agents" button.

    ``force=True`` runs the full pipeline even when review drafts already
    exist (used by the dashboard to re-run on demand).

    Returns the list of ReviewDraft objects created by ``draft_review``.
    """
    import contextlib

    import httpx as _httpx

    from franktheunicorn.config.loader import get_operator_config
    from franktheunicorn.review.drafter import draft_review

    if operator_config is None:
        with contextlib.suppress(Exception):
            operator_config = get_operator_config()

    close_http = False
    if diff_http is None:
        diff_http = _httpx.Client()
        close_http = True

    cr_config: CodeRabbitConfig | None = None
    if operator_config is not None and operator_config.coderabbit.enabled:
        cr_config = operator_config.coderabbit

    claude_cli_config: ClaudeCLIConfig | None = None
    if operator_config is not None and operator_config.claude_cli.enabled:
        claude_cli_config = operator_config.claude_cli

    snowflake_config: SnowflakeReviewConfig | None = None
    if operator_config is not None and operator_config.snowflake_review.enabled:
        snowflake_config = operator_config.snowflake_review

    def _log(msg: str) -> None:
        logger.info(msg)
        if log_lines is not None:
            log_lines.append(msg)

    try:
        if not force and pr.review_drafts.exists():
            return []

        if not force and pr.queue == "wip":
            logger.debug("PR #%d is in the wip queue; skipping review pipeline.", pr.number)
            return []

        _log(f"Starting agent run for PR #{pr.number}: {pr.title}")

        community_ctx = ""
        jira_ctx = ""
        sentry_ctx = ""
        try:
            from franktheunicorn.data_access.context_orchestrator import (
                fetch_community_context,
                fetch_jira_context,
                fetch_sentry_context,
            )

            _log("Fetching external context (JIRA, community, Sentry)...")
            jira_ctx = fetch_jira_context(pr, pc, http_client=diff_http)
            community_ctx = fetch_community_context(pr, pc, operator_config, http_client=diff_http)
            sentry_ctx = fetch_sentry_context(pr, operator_config, http_client=diff_http)
        except Exception:
            logger.debug("External context fetch failed for PR #%d", pr.number, exc_info=True)

        # Secondary rescore: apply mailing list signals now that community context is populated.
        if community_ctx:
            try:
                from franktheunicorn.data_access.jira.fetcher import extract_ticket_ids
                from franktheunicorn.scoring.signals import (
                    score_mailing_list_blame_author,
                    score_mailing_list_mention,
                )

                pr_ids: set[str] = set()
                if pr.jira_ticket_id:
                    pr_ids.add(pr.jira_ticket_id)
                if pr.number:
                    pr_ids.add(f"#{pr.number}")
                import contextlib

                with contextlib.suppress(Exception):
                    pr_ids.update(extract_ticket_ids(f"{pr.title} {pr.body}", project_prefix=""))

                ml_boost = score_mailing_list_mention(pr.community_context_cache, pr_ids) or 0
                blame_boost = score_mailing_list_blame_author(pr.community_context_cache) or 0
                if ml_boost or blame_boost:
                    pr.interest_score = min(100.0, pr.interest_score + ml_boost + blame_boost)
                    breakdown: dict[str, object] = dict(pr.score_breakdown or {})
                    if ml_boost:
                        breakdown["mailing_list_mention"] = ml_boost
                    if blame_boost:
                        breakdown["mailing_list_blame_author"] = blame_boost
                    pr.score_breakdown = breakdown
                    pr.save(update_fields=["interest_score", "score_breakdown", "updated_at"])
                    _log(
                        f"Mailing list rescore: +{ml_boost + blame_boost} "
                        f"(mention={ml_boost}, blame={blame_boost})"
                    )
            except Exception:
                logger.debug("Mailing list rescore failed for PR #%d", pr.number, exc_info=True)

        health_ctx = _build_repo_health_context(pr)

        effective_config = operator_config
        if disabled_backends and operator_config is not None:
            active = [
                bc
                for i, bc in enumerate(operator_config.llm_backends)
                if i not in disabled_backends
            ]
            effective_config = operator_config.model_copy(update={"llm_backends": active})

        # Fetch the PR's real unified diff for the LLM pipeline. Without it,
        # draft_review falls back to a filename-only placeholder and the
        # backends never see the actual changes. Failure degrades gracefully
        # to that placeholder (e.g. non-GitHub forges).
        pr_diff = ""
        try:
            from franktheunicorn.data_access.github.diff_fetcher import DiffFetcher

            _log("Fetching PR diff...")
            pr_diff = DiffFetcher(client=diff_http).fetch(pc.owner, pc.repo, pr.number).raw_diff
        except Exception:
            logger.debug(
                "Diff fetch failed for PR #%d; using changed-files placeholder.",
                pr.number,
                exc_info=True,
            )

        _log("Running LLM review pipeline...")
        drafts = draft_review(
            pr,
            pc,
            operator_config=effective_config,
            diff=pr_diff,
            repo_health_context=health_ctx,
            community_context=community_ctx,
            jira_context=jira_ctx,
            sentry_context=sentry_ctx,
            repo_path=repo_path,
        )
        _log(f"LLM review complete: {len(drafts)} finding(s) generated")
        logger.info(
            "  PR #%d: score=%.2f, %d drafts generated",
            pr.number,
            pr.interest_score,
            len(drafts),
        )

        clone_url = _clone_url_for_project(pc, operator_config)

        if cr_config is not None:
            _log("Running CodeRabbit...")
            _run_coderabbit_for_pr(
                pr,
                cr_config,
                repo_path,
                clone_url,
                project_config=pc,
                operator_config=effective_config,
                diff_http=diff_http,
            )
        if claude_cli_config is not None:
            _log("Running Claude CLI...")
            _run_claude_cli_for_pr(
                pr,
                claude_cli_config,
                repo_path,
                clone_url,
                project_config=pc,
                operator_config=effective_config,
                diff_http=diff_http,
            )
        if snowflake_config is not None:
            _log("Running Snowflake review...")
            _run_snowflake_for_pr(
                pr,
                snowflake_config,
                repo_path,
                clone_url,
                project_config=pc,
                operator_config=effective_config,
                diff_http=diff_http,
            )

        if pc.llm_checks:
            try:
                from franktheunicorn.data_access.github.diff_fetcher import DiffFetcher
                from franktheunicorn.review.checks import run_enabled_checks

                _log("Running LLM checks...")
                check_diff = pr_diff
                if not check_diff:
                    check_diff = (
                        DiffFetcher(client=diff_http).fetch(pc.owner, pc.repo, pr.number).raw_diff
                    )
                check_drafts = run_enabled_checks(
                    pr,
                    check_diff,
                    project_config=pc,
                    operator_config=operator_config,
                    repo_path=repo_path,
                )
                if check_drafts:
                    _log(f"LLM checks: {len(check_drafts)} finding(s)")
                    logger.info("  PR #%d: %d LLM check findings", pr.number, len(check_drafts))
            except Exception:
                logger.exception("Error in LLM checks for PR #%d", pr.number)

        _log("Agent run complete.")
        return drafts
    finally:
        if close_http:
            diff_http.close()


def _run_cycle(
    clients: Mapping[str, object],
    project_configs: Sequence[object],
    operator_username: str,
    operator_config: OperatorConfig | None = None,
    disabled_backends: frozenset[int] = frozenset(),
) -> None:
    """Run one polling cycle across all configured projects.

    ``clients`` maps forge-name → ``ForgeClient``. Projects are dispatched
    to their respective forge client based on ``ProjectConfig.forge``.
    Projects whose forge is not in the map are skipped with a warning.
    ``disabled_backends`` is a set of indices into ``operator_config.llm_backends``
    that failed preflight and should be excluded for this run.
    """
    import httpx
    from django.conf import settings

    from franktheunicorn.backends.poller import poll_project
    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.data_access.github.diff_fetcher import DiffFetcher
    from franktheunicorn.review.copypasta import check_copypasta
    from franktheunicorn.worker.test_runner import TestRunner

    # Shared HTTP client for diff fetching (copypasta + dependency changelogs).
    # NOTE: DiffFetcher is currently GitHub-specific (talks to api.github.com).
    # For non-GitHub projects, the LLM-checks/copypasta/changelog code paths
    # below will silently fall through. Tracked as a follow-up; see review
    # comment on PR #75.
    diff_http = httpx.Client()
    # Adaptive limiter (SQLite bucket + X-RateLimit-Remaining headers) for
    # this client's unauthenticated GitHub calls — per the rate-limiting
    # convention in CLAUDE.md.
    rate_limiter = None
    try:
        from django.conf import settings as _settings

        from franktheunicorn.data_access.rate_limiter import GitHubRateLimiter

        rate_limiter = GitHubRateLimiter(Path(_settings.DATA_DIR) / "rate_limits.sqlite")
    except Exception:
        logger.debug("Could not initialize GitHub rate limiter", exc_info=True)
    diff_fetcher = DiffFetcher(client=diff_http, rate_limiter=rate_limiter)
    test_runner = TestRunner()

    all_prs: list[object] = []
    pr_to_config: dict[int, ProjectConfig] = {}

    for pc in project_configs:
        if not isinstance(pc, ProjectConfig) or not pc.enabled:
            logger.debug(
                "Skipping project config %r (not a ProjectConfig or disabled)",
                getattr(pc, "owner", "?"),
            )
            continue
        client = clients.get(pc.forge)
        if client is None:
            logger.warning(
                "No client registered for forge %r; skipping %s/%s",
                pc.forge,
                pc.owner,
                pc.repo,
            )
            continue
        try:
            logger.info(
                "PR poll: %s/%s (forge=%s, as=%s)",
                pc.owner,
                pc.repo,
                pc.forge,
                operator_username or "?",
            )

            # Ensure local repo clone exists and is fetched (v1.25).
            repo_path: Path | None = None
            try:
                from franktheunicorn.worker.repo_manager import ensure_repo

                logger.info("Repo sync: %s/%s — ensuring local clone ...", pc.owner, pc.repo)
                repo_path = ensure_repo(Path(settings.FRANK_REPOS_DIR), pc.owner, pc.repo)
                logger.debug("Repo clone ready at %s", repo_path)
            except Exception:
                logger.debug(
                    "Repo checkout failed for %s/%s; blame will be skipped",
                    pc.owner,
                    pc.repo,
                    exc_info=True,
                )

            # Repo health analysis: run (or refresh) when stale or missing.
            logger.debug("Checking repo health snapshot for %s/%s ...", pc.owner, pc.repo)
            _maybe_refresh_repo_health(pc, repo_path)

            logger.debug("Calling poll_project for %s/%s ...", pc.owner, pc.repo)
            prs = poll_project(
                client=client,  # type: ignore[arg-type]
                project_config=pc,
                operator_username=operator_username,
                repo_path=repo_path,
            )
            logger.debug("poll_project returned %d PR(s) for %s/%s", len(prs), pc.owner, pc.repo)
            for pr in prs:
                all_prs.append(pr)
                pr_to_config[pr.pk] = pc
                logger.debug(
                    "Processing PR #%d (%s/%s) score=%.2f",
                    pr.number,
                    pc.owner,
                    pc.repo,
                    getattr(pr, "interest_score", 0.0),
                )

                process_pr(
                    pr,
                    pc,
                    operator_config,
                    disabled_backends=disabled_backends,
                    diff_http=diff_http,
                    repo_path=repo_path,
                )

                # Differential test verification (§9).
                try:
                    test_run = test_runner.run_differential_test(pr, pc, repo_path)
                    if test_run:
                        logger.info(
                            "  PR #%d: test verdict=%s",
                            pr.number,
                            test_run.differential_verdict or "pending",
                        )
                except Exception:
                    logger.exception("Error in test verification for PR #%d", pr.number)

                # Copy-pasta detection (runs even if drafts already exist)
                if pc.copypasta_enabled:
                    repo_path = Path(settings.FRANK_REPOS_DIR) / pc.owner / pc.repo
                    if repo_path.is_dir():
                        try:
                            diff = diff_fetcher.fetch(pc.owner, pc.repo, pr.number)
                            cp_drafts = check_copypasta(pr, diff, pc, repo_path)
                            if cp_drafts:
                                logger.info(
                                    "  PR #%d: %d copy-pasta findings",
                                    pr.number,
                                    len(cp_drafts),
                                )
                        except Exception:
                            logger.exception("Error in copy-pasta check for PR #%d", pr.number)
                    else:
                        logger.debug(
                            "Repo clone not found at %s, skipping copy-pasta check",
                            repo_path,
                        )
                logger.debug("Finished processing PR #%d (%s/%s)", pr.number, pc.owner, pc.repo)
        except Exception:
            logger.exception("Error polling %s/%s", pc.owner, pc.repo)

    # Fetch dependency changelogs reusing the same HTTP client.
    _fetch_dependency_changelogs_for_cycle(
        all_prs, pr_to_config, diff_fetcher, diff_http, rate_limiter=rate_limiter
    )

    # Shepherding pass for operator's own PRs (v2 — §2.3).
    if operator_config is not None:
        _run_shepherding_pass(all_prs, pr_to_config, operator_config)

    # Security email ingestion.
    if operator_config is not None:
        _poll_security_emails(operator_config)

    # Mention scan: ingest open PRs where the operator is involved but that
    # aren't in a configured project (mentioned, assigned, review-requested).
    if operator_username:
        _scan_mentioned_prs(clients, operator_username, operator_config)

    # Backfill pass: draft reviews for open PRs that were ingested (e.g. via
    # lookup_pr) but never reached the main poll loop above.
    _backfill_unreviewed_prs(
        already_polled_pks={getattr(pr, "pk", None) for pr in all_prs},
        project_configs=project_configs,
        operator_config=operator_config,
        disabled_backends=disabled_backends,
        diff_http=diff_http,
    )

    diff_http.close()


def _scan_mentioned_prs(
    clients: Mapping[str, object],
    operator_username: str,
    operator_config: OperatorConfig | None,
) -> None:
    """Ingest open PRs where the operator is involved (mentioned/assigned/review-requested).

    Iterates every configured forge client and calls ``search_prs_involving``.
    Each found PR is ingested via ``ingest_single_pr`` (idempotent upsert).
    Failures per-PR are caught individually so one bad PR doesn't stop the rest.
    """
    from franktheunicorn.backends.poller import ingest_single_pr

    total_found = 0
    total_ingested = 0

    for _forge_name, client in clients.items():
        if not hasattr(client, "search_prs_involving"):
            continue
        try:
            items = client.search_prs_involving(operator_username)
        except Exception:
            logger.debug("search_prs_involving failed for forge %s", _forge_name, exc_info=True)
            continue

        total_found += len(items)
        for item in items:
            # Skip plain issues — only process actual PRs.
            if not item.get("pull_request"):
                continue

            # Parse owner/repo from repository_url:
            # e.g. "https://api.github.com/repos/apache/spark"
            repo_url: str = item.get("repository_url", "")
            parts = repo_url.rstrip("/").rsplit("/", 2)
            if len(parts) < 3:
                logger.debug("Could not parse repository_url %r; skipping", repo_url)
                continue
            owner, repo = parts[-2], parts[-1]
            pr_number: int | None = item.get("number")
            if not pr_number:
                continue

            try:
                ingest_single_pr(owner, repo, pr_number)
                total_ingested += 1
            except Exception:
                logger.debug(
                    "Failed to ingest mentioned PR %s/%s#%d",
                    owner,
                    repo,
                    pr_number,
                    exc_info=True,
                )

    if total_found:
        logger.info(
            "Mention scan: %d PR(s) found involving %s, %d ingested/refreshed.",
            total_found,
            operator_username,
            total_ingested,
        )


def _backfill_unreviewed_prs(
    already_polled_pks: set[int | None],
    project_configs: Sequence[object],
    operator_config: OperatorConfig | None,
    disabled_backends: frozenset[int],
    diff_http: httpx.Client,
) -> None:
    """Draft reviews for open PRs in the DB that have no review drafts yet.

    Handles PRs ingested via lookup_pr or other paths that bypass the normal
    poll cycle (which only sees PRs currently open on the forge).
    """
    from django.db.models import Count

    from franktheunicorn.config.loader import get_project_config
    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.core.models import PullRequest as PullRequestModel

    backfill_qs = (
        PullRequestModel.objects.filter(state="open")
        .exclude(pk__in={pk for pk in already_polled_pks if pk is not None})
        .exclude(queue="wip")
        .annotate(draft_count=Count("review_drafts"))
        .filter(draft_count=0)
        .select_related("project")
    )

    # Build a quick lookup: full_name → ProjectConfig, from the configs that
    # were already loaded for this cycle.
    config_by_name: dict[str, ProjectConfig] = {}
    for pc in project_configs:
        if isinstance(pc, ProjectConfig) and pc.enabled:
            config_by_name[f"{pc.owner}/{pc.repo}"] = pc

    for pr in backfill_qs:
        full_name = pr.project.full_name if hasattr(pr.project, "full_name") else str(pr.project)
        pc = config_by_name.get(full_name) or get_project_config(full_name)
        if not isinstance(pc, ProjectConfig):
            logger.debug(
                "No project config for %s, skipping backfill of PR #%d", full_name, pr.number
            )
            continue

        logger.info("Backfilling review for PR #%d (%s)", pr.number, full_name)
        try:
            drafts = process_pr(
                pr,
                pc,
                operator_config,
                disabled_backends=disabled_backends,
                diff_http=diff_http,
            )
            logger.info("  Backfill PR #%d: %d drafts generated", pr.number, len(drafts))
        except Exception:
            logger.exception("Error backfilling review for PR #%d", pr.number)


def _run_shepherding_pass(
    all_prs: list[object],
    pr_to_config: Mapping[int, object],
    operator_config: OperatorConfig,
) -> None:
    """Run shepherding on operator's own PRs with new reviewer comments."""
    from django.utils import timezone

    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.core.models import PullRequest as PullRequestModel
    from franktheunicorn.review.shepherding import (
        generate_shepherd_drafts,
    )

    for pr in all_prs:
        if not isinstance(pr, PullRequestModel):
            continue
        if not pr.is_operator_pr:
            continue

        pc = pr_to_config.get(pr.pk)
        if not isinstance(pc, ProjectConfig):
            continue

        # Shepherding is a v2 feature — run only when the project opted in.
        if not pc.shepherding_enabled:
            continue

        try:
            # Skip if already shepherded recently (within the poll interval).
            shepherd_throttle = operator_config.poll_interval_seconds or 300
            if (
                pr.last_shepherded_at
                and (timezone.now() - pr.last_shepherded_at).total_seconds() < shepherd_throttle
            ):
                continue

            # Check for new reviewer comments via the existing review count field.
            # In a full implementation, this would fetch from GitHub API.
            # For now, generate condition alerts (rebase, staleness) which
            # don't require fetching comments.
            drafts = generate_shepherd_drafts(
                pr,
                [],  # No comments fetched yet — condition alerts only.
                operator_config,
                pc,
            )

            if drafts:
                logger.info(
                    "  PR #%d: %d shepherding findings",
                    pr.number,
                    len(drafts),
                )

            pr.last_shepherded_at = timezone.now()
            pr.save(update_fields=["last_shepherded_at", "updated_at"])
        except Exception:
            logger.exception("Error in shepherding for PR #%d", pr.number)


# API path suffixes that forge clients accept on ``base_url`` and normalize
# internally. Clone URLs need the bare web host, so strip these.
_FORGE_API_SUFFIXES: tuple[str, ...] = ("/api/v1", "/api/v3", "/api/v4", "/api")


def _strip_forge_api_suffix(base_url: str) -> str:
    """Trim a known API path suffix from a forge ``base_url``.

    Mirrors the ``_normalize_base_url`` logic in the forge clients so we
    don't accidentally bake ``/api/v1`` (gitea/forgejo), ``/api/v4``
    (gitlab), or ``/api/v3`` (github enterprise) into a clone URL.
    """
    base = base_url.rstrip("/")
    for suffix in _FORGE_API_SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _clone_url_for_project(
    pc: object,
    operator_config: OperatorConfig | None,
) -> str:
    """Derive a clone URL override from the project's forge entry.

    Returns ``""`` when the executor's ``clone_url_template`` already
    suffices (default GitHub, or no forge info available). Returns an
    explicit URL only when the forge demands one — non-GitHub forges
    (gitlab/gitea/forgejo) and self-hosted GitHub Enterprise.

    Returning ``""`` for the common case preserves the
    ``RemoteExecutionConfig.clone_url_template`` config knob: operators
    can still set it to e.g. an SSH-style ``git@github.com:{owner}/{repo}.git``
    template and have it apply.
    """
    owner = getattr(pc, "owner", "")
    repo = getattr(pc, "repo", "")
    forge_name = getattr(pc, "forge", "") or "github"

    forge = None
    if operator_config is not None:
        for entry in operator_config.forges:
            if entry.name == forge_name:
                forge = entry
                break

    if forge is None:
        # No forge config at all — let the template default apply.
        return ""

    base = _strip_forge_api_suffix(forge.base_url or "")

    if forge.type == "github":
        # Public github at the default API URL → template default works.
        if not base or base == "https://api.github.com":
            return ""
        # GitHub Enterprise — must override.
        return f"{base}/{owner}/{repo}.git"

    # gitlab / gitea / forgejo all need the web host. Empty base_url is
    # invalid for these forges (validated upstream for gitea); fall back
    # to template default rather than emit a malformed URL.
    if not base:
        return ""
    return f"{base}/{owner}/{repo}.git"


def _checkout_pr_head_with_merge(
    executor: object,
    cwd: str,
    pr: PullRequest,
    base_ref: str,
) -> tuple[bool, str | None]:
    """Fetch, create a temp branch on head_sha, and merge base_ref into it.

    Returns ``(ok, temp_branch_name)``:
    - ``(False, None)``: fatal failure (fetch or checkout failed); caller should abort.
    - ``(True, branch_name)``: merge succeeded; caller must clean up via
      ``_cleanup_review_branch`` after the review tools finish.
    - ``(True, None)``: merge conflict; caller should skip the local tool and
      fall back to the GitHub diff.

    The temp branch is named ``franktheunicorn-review-{pr.number}`` to avoid
    collisions with real branches.
    """
    head_sha = (pr.head_sha or "").strip()
    if not head_sha:
        logger.debug("PR #%d has no head_sha; cannot checkout head for review.", pr.number)
        return False, None

    fetch = executor.run(  # type: ignore[attr-defined]
        ["git", "fetch", "--quiet", "origin", head_sha],
        cwd=cwd,
        timeout=300,
    )
    if fetch is None or not fetch.ok:
        logger.warning(
            "Failed to fetch PR #%d head %s in %s; review tools will be skipped.",
            pr.number,
            head_sha[:12],
            cwd,
        )
        return False, None

    branch_name = f"franktheunicorn-review-{pr.number}"
    # -B (force-reset) rather than -b: a worker killed mid-review leaves the
    # branch behind, and -b would then fail every future cycle for this PR.
    checkout = executor.run(  # type: ignore[attr-defined]
        ["git", "checkout", "-B", branch_name, head_sha],
        cwd=cwd,
        timeout=30,
    )
    if checkout is None or not checkout.ok:
        logger.warning(
            "Failed to create review branch for PR #%d in %s; review tools will be skipped.",
            pr.number,
            cwd,
        )
        return False, None

    merge = executor.run(  # type: ignore[attr-defined]
        ["git", "merge", "--no-edit", "--no-ff", base_ref],
        cwd=cwd,
        timeout=120,
    )
    if merge is None or not merge.ok:
        logger.warning(
            "PR #%d: merge conflict with %s; skipping local tool, will fall back to GitHub diff.",
            pr.number,
            base_ref,
        )
        # Abort the failed merge and clean up the temp branch before returning.
        executor.run(["git", "merge", "--abort"], cwd=cwd, timeout=15)  # type: ignore[attr-defined]
        executor.run(["git", "checkout", "--detach", head_sha], cwd=cwd, timeout=15)  # type: ignore[attr-defined]
        executor.run(["git", "branch", "-D", branch_name], cwd=cwd, timeout=15)  # type: ignore[attr-defined]
        return True, None

    return True, branch_name


def _cleanup_review_branch(executor: object, cwd: str, temp_branch: str) -> None:
    """Detach HEAD and delete the temporary review branch created by _checkout_pr_head_with_merge."""
    executor.run(["git", "checkout", "--detach", "HEAD"], cwd=cwd, timeout=15)  # type: ignore[attr-defined]
    executor.run(["git", "branch", "-D", temp_branch], cwd=cwd, timeout=15)  # type: ignore[attr-defined]


def _resolve_cwd_for_tool(
    pr: PullRequest,
    remote_config: object,
    local_repo_path: Path | None,
    tool_name: str,
    clone_url: str = "",
) -> tuple[str, str, str | None] | None:
    """
    Resolve a working directory + base ref for one of the CLI review tools.

    Returns ``(cwd, base_ref, temp_branch)`` or ``None`` when no checkout
    could be prepared:
    - ``None``: fatal failure; caller should skip.
    - ``(cwd, base_ref, branch_name)``: ready to run; caller must call
      ``_cleanup_review_branch`` after the tool finishes.
    - ``(cwd, base_ref, None)``: merge conflict (local path only); caller
      should skip the local tool and fall back to GitHub diff.
    - ``(cwd, base_ref, _REMOTE)``: remote execution; no local branch was
      created and no cleanup is needed.

    For remote execution (SSH), ``temp_branch`` is the ``_REMOTE`` sentinel —
    no merge-before-diff is attempted remotely and no cleanup is needed.
    """
    from franktheunicorn.review.tool_executor import (
        LocalExecutor,
        RemoteSSHExecutor,
        make_executor,
    )

    executor = make_executor(remote_config)  # type: ignore[arg-type]

    if isinstance(executor, LocalExecutor):
        if local_repo_path is None or not local_repo_path.exists():
            logger.debug(
                "Repo clone unavailable for %s; skipping %s for PR #%d",
                pr.project.full_name,
                tool_name,
                pr.number,
            )
            return None
        base_ref = _resolve_base_ref(local_repo_path, pr)
        if base_ref is None:
            return None
        ok, temp_branch = _checkout_pr_head_with_merge(executor, str(local_repo_path), pr, base_ref)
        if not ok:
            return None
        return str(local_repo_path), base_ref, temp_branch

    # Remote execution: clone (or fetch) the repo on the remote host.
    # No merge-before-diff remotely — returns _REMOTE sentinel as temp_branch.
    if not isinstance(executor, RemoteSSHExecutor):
        logger.debug(
            "Unexpected executor type %s for remote path; skipping %s.", type(executor), tool_name
        )
        return None
    remote_cwd = executor.prepare_repo(
        pr.project.owner,
        pr.project.repo,
        clone_url=clone_url,
    )
    if remote_cwd is None:
        logger.debug(
            "Remote prepare_repo failed for %s; skipping %s for PR #%d",
            pr.project.full_name,
            tool_name,
            pr.number,
        )
        return None
    base_ref = _resolve_remote_base_ref(executor, remote_cwd, pr)
    if base_ref is None:
        return None
    # Remote: checkout head but don't attempt merge (no conflict tracking).
    head_sha = (pr.head_sha or "").strip()
    if head_sha:
        fetch = executor.run(
            ["git", "fetch", "--quiet", "origin", head_sha], cwd=remote_cwd, timeout=300
        )
        if fetch is None or not fetch.ok:
            logger.warning(
                "Failed to fetch PR #%d head in remote %s; skipping %s.",
                pr.number,
                remote_cwd,
                tool_name,
            )
            return None
        checkout = executor.run(
            ["git", "checkout", "--quiet", "--detach", head_sha], cwd=remote_cwd, timeout=30
        )
        if checkout is None or not checkout.ok:
            logger.warning(
                "Failed to checkout PR #%d head in remote %s; skipping %s.",
                pr.number,
                remote_cwd,
                tool_name,
            )
            return None
    return remote_cwd, base_ref, _REMOTE


def _handle_merge_conflict(
    pr: PullRequest,
    project_config: ProjectConfig | None,
    operator_config: OperatorConfig | None,
    diff_http: httpx.Client | None,
) -> None:
    """Mark a PR non-mergeable and ensure the rebase-needed draft exists.

    The main pipeline in ``process_pr`` has already run the GitHub-diff
    review for this PR, so no fallback re-review happens here — re-running
    ``draft_review`` per conflicting CLI tool duplicated every LLM finding
    (and its cost) once per tool.
    """
    from franktheunicorn.review.drafter import ensure_conflict_draft

    if pr.mergeable is not False:
        pr.mergeable = False
        pr.save(update_fields=["mergeable", "updated_at"])
    try:
        ensure_conflict_draft(pr)
    except Exception:
        logger.exception("Failed to create conflict draft for PR #%d", pr.number)


def _run_review_tool_for_pr(
    pr: PullRequest,
    tool_name: str,
    remote_config: object,
    repo_path: Path | None,
    clone_url: str,
    project_config: ProjectConfig | None,
    operator_config: OperatorConfig | None,
    diff_http: httpx.Client | None,
    run_review: Callable[..., list[Any]],
    create_drafts: Callable[..., list[Any]],
    tool_config: object,
) -> None:
    """Shared scaffold for all CLI review tool runners. Never raises.

    Resolves a working directory, handles merge conflicts and remote execution,
    calls ``run_review``, converts findings to drafts, and cleans up the temp
    branch on exit.
    """
    from franktheunicorn.review.tool_executor import make_executor

    resolved = _resolve_cwd_for_tool(pr, remote_config, repo_path, tool_name, clone_url=clone_url)
    if resolved is None:
        return
    cwd, base_ref, temp_branch = resolved

    if temp_branch is None:
        _handle_merge_conflict(pr, project_config, operator_config, diff_http)
        return

    executor = make_executor(remote_config)  # type: ignore[arg-type]
    real_branch: str | None = None if temp_branch == _REMOTE else temp_branch
    try:
        findings = run_review(cwd, base_ref, tool_config, executor=executor)
        if findings:
            drafts = create_drafts(pr, findings, pr.project, diff_source="local_git_merged")
            logger.info(
                "  PR #%d: %d %s findings → %d drafts",
                pr.number,
                len(findings),
                tool_name,
                len(drafts),
            )
    except Exception:
        logger.exception("%s failed for PR #%d; continuing.", tool_name, pr.number)
    finally:
        if real_branch is not None:
            _cleanup_review_branch(executor, cwd, real_branch)


def _run_coderabbit_for_pr(
    pr: PullRequest,
    cr_config: CodeRabbitConfig,
    repo_path: Path | None,
    clone_url: str = "",
    project_config: ProjectConfig | None = None,
    operator_config: OperatorConfig | None = None,
    diff_http: httpx.Client | None = None,
) -> None:
    """Run CodeRabbit CLI review for a single PR. Never raises."""
    from franktheunicorn.review.coderabbit import (
        create_drafts_from_coderabbit,
        run_coderabbit_review,
    )

    _run_review_tool_for_pr(
        pr,
        "CodeRabbit",
        cr_config.remote,
        repo_path,
        clone_url,
        project_config,
        operator_config,
        diff_http,
        run_review=run_coderabbit_review,
        create_drafts=create_drafts_from_coderabbit,
        tool_config=cr_config,
    )


def _run_claude_cli_for_pr(
    pr: PullRequest,
    claude_config: ClaudeCLIConfig,
    repo_path: Path | None,
    clone_url: str = "",
    project_config: ProjectConfig | None = None,
    operator_config: OperatorConfig | None = None,
    diff_http: httpx.Client | None = None,
) -> None:
    """Run the Claude CLI review for a single PR. Never raises."""
    from franktheunicorn.review.claude_cli import (
        create_drafts_from_claude_cli,
        run_claude_cli_review,
    )

    _run_review_tool_for_pr(
        pr,
        "Claude CLI",
        claude_config.remote,
        repo_path,
        clone_url,
        project_config,
        operator_config,
        diff_http,
        run_review=run_claude_cli_review,
        create_drafts=create_drafts_from_claude_cli,
        tool_config=claude_config,
    )


def _run_snowflake_for_pr(
    pr: PullRequest,
    snowflake_config: SnowflakeReviewConfig,
    repo_path: Path | None,
    clone_url: str = "",
    project_config: ProjectConfig | None = None,
    operator_config: OperatorConfig | None = None,
    diff_http: httpx.Client | None = None,
) -> None:
    """Run the Snowflake code review CLI for a single PR. Never raises."""
    from franktheunicorn.review.snowflake_review import (
        create_drafts_from_snowflake,
        run_snowflake_review,
    )

    _run_review_tool_for_pr(
        pr,
        "Snowflake review",
        snowflake_config.remote,
        repo_path,
        clone_url,
        project_config,
        operator_config,
        diff_http,
        run_review=run_snowflake_review,
        create_drafts=create_drafts_from_snowflake,
        tool_config=snowflake_config,
    )


def _resolve_remote_base_ref(
    executor: object,
    remote_cwd: str,
    pr: PullRequest,
) -> str | None:
    """Mirror of ``_resolve_base_ref`` for a remote checkout (over SSH)."""
    from franktheunicorn.review.tool_executor import RemoteSSHExecutor

    if not isinstance(executor, RemoteSSHExecutor):
        return None

    for candidate in ("origin/main", "origin/master"):
        result = executor.run(
            ["git", "rev-parse", "--verify", candidate],
            cwd=remote_cwd,
            timeout=15,
        )
        if result is not None and result.ok:
            return candidate

    logger.debug(
        "Could not determine remote base ref for PR #%d in %s; skipping.",
        pr.number,
        remote_cwd,
    )
    return None


def _resolve_base_ref(repo_path: Path, pr: PullRequest) -> str | None:
    """
    Try to determine the base ref for CodeRabbit diffing.

    Returns ``None`` (and logs) when we can't determine a sensible base.
    """
    import subprocess

    for candidate in ("origin/main", "origin/master"):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", candidate],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
        )
        if result.returncode == 0:
            return candidate

    logger.debug(
        "Could not determine base ref for PR #%d in %s; skipping CodeRabbit.",
        pr.number,
        repo_path,
    )
    return None


_last_security_email_poll: float = 0.0


def _poll_security_emails(operator_config: OperatorConfig) -> None:
    """Poll security email inbox and create SecurityReport records."""
    global _last_security_email_poll

    if not operator_config.security_triage.enabled:
        return
    if not operator_config.security_triage.email.enabled:
        return

    # Respect the configured poll interval.
    now = time.monotonic()
    interval = operator_config.security_triage.email.poll_interval_seconds
    if now - _last_security_email_poll < interval:
        return

    try:
        from franktheunicorn.core.models import SecurityReport
        from franktheunicorn.data_access.email_inbox.fetcher import fetch_security_emails

        messages = fetch_security_emails(operator_config.security_triage.email)
        for msg in messages:
            # Skip if already ingested (by message-id).
            if (
                msg.message_id
                and SecurityReport.objects.filter(email_message_id=msg.message_id).exists()
            ):
                continue

            report = SecurityReport.objects.create(
                raw_text=msg.body,
                title=msg.subject,
                reporter_name=msg.from_name,
                reporter_email=msg.from_email,
                source="email",
                email_message_id=msg.message_id,
                email_received_at=msg.received_at,
            )
            logger.info("Ingested security report from email: %s", msg.subject)

            # Auto-triage if configured.
            if operator_config.security_triage.auto_triage:
                try:
                    from franktheunicorn.security.triage import triage_report

                    triage_report(report, None, operator_config)
                except Exception:
                    logger.exception("Auto-triage failed for email report %d", report.pk)
        # Only update timestamp after successful poll so errors retry sooner.
        _last_security_email_poll = now
    except Exception:
        logger.exception("Error polling security emails")


def _fetch_dependency_changelogs_for_cycle(
    prs: list[object],
    project_configs_by_pr: Mapping[int, object],
    diff_fetcher: DiffFetcher,
    http_client: httpx.Client,
    rate_limiter: object | None = None,
) -> None:
    """Fetch dependency changelogs for all PRs in a cycle that touch dependency files."""
    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.core.models import PullRequest as PullRequestModel
    from franktheunicorn.data_access.dependencies.registry import is_dependency_file

    # Filter to PRs that need changelog fetching
    eligible: list[tuple[PullRequestModel, ProjectConfig]] = []
    for pr in prs:
        if not isinstance(pr, PullRequestModel):
            continue
        pc = project_configs_by_pr.get(pr.pk)
        if not isinstance(pc, ProjectConfig):
            continue
        changed_files: list[str] = pr.changed_files or []
        if not any(is_dependency_file(f) for f in changed_files):
            continue
        if pr.dependency_changes.exists():
            continue
        eligible.append((pr, pc))

    if not eligible:
        return

    try:
        from franktheunicorn.data_access.dependencies.service import (
            detect_and_fetch_changelogs,
        )

        for pr, pc in eligible:
            try:
                diff = diff_fetcher.fetch(pc.owner, pc.repo, pr.number)
                detect_and_fetch_changelogs(pr, diff, http_client, rate_limiter=rate_limiter)  # type: ignore[arg-type]
            except Exception:
                logger.exception(
                    "Error fetching dependency changelogs for PR #%d",
                    pr.number,
                )
    except Exception:
        logger.exception("Error in dependency changelog processing")


if __name__ == "__main__":
    run_worker(sys.argv[1:])
