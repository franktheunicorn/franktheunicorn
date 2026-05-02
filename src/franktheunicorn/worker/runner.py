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
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import django

if TYPE_CHECKING:
    import httpx

    from franktheunicorn.config.models import CodeRabbitConfig, OperatorConfig
    from franktheunicorn.core.models import PullRequest
    from franktheunicorn.data_access.github.diff_fetcher import DiffFetcher

logger = logging.getLogger(__name__)

_VALID_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET")


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
    from franktheunicorn.config.resolver import get_forge_entry

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

    operator_config = load_operator_config(settings.FRANK_OPERATOR_CONFIG)
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
    logger.info("Worker starting. Poll interval: %ds", poll_interval)

    try:
        while True:
            _run_cycle(clients, project_configs, operator_config.github_username, operator_config)
            logger.info("Sleeping %ds until next poll...", poll_interval)
            time.sleep(poll_interval)
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


def _run_cycle(
    clients: Mapping[str, object],
    project_configs: Sequence[object],
    operator_username: str,
    operator_config: OperatorConfig | None = None,
) -> None:
    """Run one polling cycle across all configured projects.

    ``clients`` maps forge-name → ``ForgeClient``. Projects are dispatched
    to their respective forge client based on ``ProjectConfig.forge``.
    Projects whose forge is not in the map are skipped with a warning.
    """
    import httpx
    from django.conf import settings

    from franktheunicorn.backends.poller import poll_project
    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.data_access.github.diff_fetcher import DiffFetcher
    from franktheunicorn.review.copypasta import check_copypasta
    from franktheunicorn.review.drafter import draft_review
    from franktheunicorn.worker.test_runner import TestRunner

    # Resolve CodeRabbit config from operator config.
    cr_config: CodeRabbitConfig | None = None
    if operator_config is not None and operator_config.coderabbit.enabled:
        cr_config = operator_config.coderabbit

    # Shared HTTP client for diff fetching (copypasta + dependency changelogs).
    # NOTE: DiffFetcher is currently GitHub-specific (talks to api.github.com).
    # For non-GitHub projects, the LLM-checks/copypasta/changelog code paths
    # below will silently fall through. Tracked as a follow-up; see review
    # comment on PR #75.
    diff_http = httpx.Client()
    diff_fetcher = DiffFetcher(client=diff_http)
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
            logger.info("Polling %s/%s (forge=%s) ...", pc.owner, pc.repo, pc.forge)

            # Ensure local repo clone exists and is fetched (v1.25).
            repo_path: Path | None = None
            try:
                from franktheunicorn.worker.repo_manager import ensure_repo

                logger.debug("Ensuring local repo clone for %s/%s ...", pc.owner, pc.repo)
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

                # Only draft reviews for PRs without existing drafts
                if not pr.review_drafts.exists():
                    logger.debug("Drafting review for PR #%d ...", pr.number)
                    # Fetch external context (v1.5) for the review pipeline.
                    community_ctx = ""
                    jira_ctx = ""
                    sentry_ctx = ""
                    try:
                        from franktheunicorn.data_access.context_orchestrator import (
                            fetch_community_context,
                            fetch_jira_context,
                            fetch_sentry_context,
                        )

                        jira_ctx = fetch_jira_context(pr, pc, http_client=diff_http)
                        community_ctx = fetch_community_context(
                            pr,
                            pc,
                            operator_config,
                            http_client=diff_http,
                        )
                        sentry_ctx = fetch_sentry_context(
                            pr,
                            operator_config,
                            http_client=diff_http,
                        )
                    except Exception:
                        logger.debug(
                            "External context fetch failed for PR #%d",
                            pr.number,
                            exc_info=True,
                        )

                    # Build repo health context for this PR's changed files.
                    health_ctx = _build_repo_health_context(pr)

                    drafts = draft_review(
                        pr,
                        pc,
                        operator_config=operator_config,
                        repo_health_context=health_ctx,
                        community_context=community_ctx,
                        jira_context=jira_ctx,
                        sentry_context=sentry_ctx,
                        repo_path=repo_path,
                    )
                    logger.info(
                        "  PR #%d: score=%.2f, %d drafts generated",
                        pr.number,
                        pr.interest_score,
                        len(drafts),
                    )

                    # Run CodeRabbit if enabled and no CR drafts exist yet.
                    if cr_config is not None:
                        _run_coderabbit_for_pr(pr, cr_config, repo_path)

                    # LLM sub-checks (coverage, etc.) — runs once alongside draft review.
                    if pc.llm_checks:
                        try:
                            from franktheunicorn.review.checks import run_enabled_checks

                            check_pr_diff = diff_fetcher.fetch(pc.owner, pc.repo, pr.number)
                            check_drafts = run_enabled_checks(
                                pr,
                                check_pr_diff.raw_diff,
                                project_config=pc,
                                operator_config=operator_config,
                                repo_path=repo_path,
                            )
                            if check_drafts:
                                logger.info(
                                    "  PR #%d: %d LLM check findings",
                                    pr.number,
                                    len(check_drafts),
                                )
                        except Exception:
                            logger.exception("Error in LLM checks for PR #%d", pr.number)

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
        except Exception:
            logger.exception("Error polling %s/%s", pc.owner, pc.repo)

    # Fetch dependency changelogs reusing the same HTTP client.
    _fetch_dependency_changelogs_for_cycle(all_prs, pr_to_config, diff_fetcher, diff_http)

    # Shepherding pass for operator's own PRs (v2 — §2.3).
    if operator_config is not None:
        _run_shepherding_pass(all_prs, pr_to_config, operator_config)

    # Security email ingestion.
    if operator_config is not None:
        _poll_security_emails(operator_config)

    diff_http.close()


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


def _run_coderabbit_for_pr(
    pr: PullRequest,
    cr_config: CodeRabbitConfig,
    repo_path: Path | None,
) -> None:
    """Run CodeRabbit CLI review for a single PR. Never raises.

    ``repo_path`` should be the path returned by ``ensure_repo`` for this
    project. When ``None`` or missing on disk, CodeRabbit is skipped silently.
    """
    if repo_path is None or not repo_path.exists():
        logger.debug(
            "Repo clone unavailable for %s; skipping CodeRabbit for PR #%d",
            pr.project.full_name,
            pr.number,
        )
        return

    from franktheunicorn.review.coderabbit import (
        create_drafts_from_coderabbit,
        run_coderabbit_review,
    )

    try:
        # Determine the base ref for diffing. The repo manager keeps the
        # working tree on the default branch, so origin/main or origin/master
        # should be available.
        base_ref = _resolve_base_ref(repo_path, pr)
        if base_ref is None:
            return

        findings = run_coderabbit_review(repo_path, base_ref, cr_config)
        if findings:
            drafts = create_drafts_from_coderabbit(pr, findings, pr.project)
            logger.info(
                "  PR #%d: %d CodeRabbit findings → %d drafts",
                pr.number,
                len(findings),
                len(drafts),
            )
    except Exception:
        logger.exception("CodeRabbit failed for PR #%d; continuing.", pr.number)


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
                detect_and_fetch_changelogs(pr, diff, http_client)
            except Exception:
                logger.exception(
                    "Error fetching dependency changelogs for PR #%d",
                    pr.number,
                )
    except Exception:
        logger.exception("Error in dependency changelog processing")


if __name__ == "__main__":
    run_worker(sys.argv[1:])
