"""
PR polling service.

Loads configured projects, fetches PRs from a forge backend, scores them,
and stores results in the database. Works with any ``ForgeClient``
(GitHub, Forgejo, mock).
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from django.db import transaction

from franktheunicorn.backends.base import ForgeClient
from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import Project, PullRequest, ReviewDraft
from franktheunicorn.core.session_detector import detect_agent_session
from franktheunicorn.scoring.scorer import score_pull_request_from_model

logger = logging.getLogger(__name__)

# YAML governance value → Project.project_type choice.
_GOVERNANCE_TO_PROJECT_TYPE = {"asf": "asf", "personal": "personal", "corporate": "org"}
_CONTRIBUTORS_CACHE_FETCHED = "fetched"
_CONTRIBUTORS_CACHE_EMPTY = "empty"
_CONTRIBUTORS_CACHE_FAILED = "failed"


def poll_project(
    client: ForgeClient,
    project_config: ProjectConfig,
    operator_username: str,
    *,
    repo_path: Path | None = None,
) -> list[PullRequest]:
    """
    Poll a single project for PRs, score them, and store in the DB.

    If repo_path is provided and points to a valid git clone, blame data
    is fetched for changed files and passed to the scorer.

    Returns the list of PullRequest objects that were created or updated.
    """
    project, _created = Project.objects.update_or_create(
        owner=project_config.owner,
        repo=project_config.repo,
        defaults={
            "review_context": project_config.review_context,
            # Sync the dashboard's type filter from YAML governance — nothing
            # else ever wrote this field, so the filter was dead on arrival.
            "project_type": _GOVERNANCE_TO_PROJECT_TYPE.get(project_config.governance, "personal"),
        },
    )

    logger.debug("Listing pull requests for %s/%s ...", project_config.owner, project_config.repo)
    raw_prs = client.list_pull_requests(project_config.owner, project_config.repo)
    logger.debug(
        "GitHub returned %d open PR(s) for %s/%s",
        len(raw_prs),
        project_config.owner,
        project_config.repo,
    )

    _refresh_contributors_cache(client, project, project_config.owner, project_config.repo)

    results: list[PullRequest] = []

    for pr_data in raw_prs:
        pr_number: int = pr_data["number"]
        logger.debug("Fetching changed files for PR #%d ...", pr_number)

        # Fetch changed files for scoring. On failure pass None (not []) so
        # the upsert preserves any previously-ingested file list instead of
        # wiping it during a rate-limit window.
        changed_files: list[str] | None
        try:
            files_data = client.get_pull_request_files(
                project_config.owner, project_config.repo, pr_number
            )
            changed_files = [f["filename"] for f in files_data]
            logger.debug("PR #%d touches %d file(s)", pr_number, len(changed_files))
        except Exception:
            logger.warning("Could not fetch files for PR #%d; preserving existing list", pr_number)
            changed_files = None

        pr_obj = _upsert_pull_request(project, pr_data, changed_files)

        # Fetch PR detail (includes mergeable status + base/head refs).
        pr_detail: dict[str, Any] = {}
        try:
            pr_detail = client.get_pull_request(
                project_config.owner, project_config.repo, pr_number
            )
            raw_mergeable = pr_detail.get("mergeable")
            if isinstance(raw_mergeable, bool):
                pr_obj.mergeable = raw_mergeable
        except Exception:
            logger.debug("Could not fetch PR detail for #%d", pr_number)

        # Extract base/head SHAs and branch refs for blame (v1.25).
        base_sha = ""
        head_sha = ""
        base_branch = ""
        head_branch = ""
        fork_clone_url = ""
        base_data = pr_detail.get("base")
        head_data = pr_detail.get("head")
        if isinstance(base_data, dict):
            base_sha = base_data.get("sha", "")
            base_branch = base_data.get("ref", "")
        if isinstance(head_data, dict):
            head_sha = head_data.get("sha", "")
            head_branch = head_data.get("ref", "")
            head_repo_data = head_data.get("repo") or {}
            fork_clone_url_raw = head_repo_data.get("clone_url", "")
            head_full_name = head_repo_data.get("full_name", "")
            is_fork = head_repo_data.get("fork", False) or (
                bool(head_full_name)
                and head_full_name != f"{project_config.owner}/{project_config.repo}"
            )
            fork_clone_url = fork_clone_url_raw if is_fork else ""

        # Persist SHAs on the PR so downstream consumers (differential test
        # runner, blame, etc.) don't need to re-hit the GitHub API.
        if base_sha and pr_obj.base_sha != base_sha:
            pr_obj.base_sha = base_sha
        if head_sha and pr_obj.head_sha != head_sha:
            pr_obj.head_sha = head_sha
        if head_branch and pr_obj.head_branch != head_branch:
            pr_obj.head_branch = head_branch

        # Fetch blame data if repo clone is available (v1.25).
        blame_data: list[dict[str, object]] | None = None
        if repo_path is not None and repo_path.is_dir() and changed_files and base_sha:
            try:
                from franktheunicorn.scoring.blame_fetcher import fetch_blame_for_files
                from franktheunicorn.worker.repo_manager import ensure_sha_fetched

                # Fetch refs if not available locally, then run blame.
                base_ok = ensure_sha_fetched(
                    repo_path,
                    base_sha,
                    branch=base_branch,
                    pr_number=pr_number,
                )
                head_ok = (
                    ensure_sha_fetched(
                        repo_path,
                        head_sha,
                        branch=head_branch,
                        pr_number=pr_number,
                        fork_clone_url=fork_clone_url,
                    )
                    if head_sha
                    else False
                )

                if base_ok and head_ok:
                    blame_data = fetch_blame_for_files(
                        repo_path, changed_files, base_ref=base_sha, head_ref=head_sha
                    )
                elif base_ok:
                    # Head not available but base is — diff will be against
                    # working tree. Only useful if repo happens to be on the
                    # right branch, so log a warning.
                    logger.debug(
                        "Head SHA %s not available locally for PR #%d; "
                        "blame diff may be inaccurate",
                        head_sha[:12],
                        pr_number,
                    )
                    blame_data = fetch_blame_for_files(repo_path, changed_files, base_ref=base_sha)
                else:
                    logger.debug(
                        "Base SHA %s not available locally for PR #%d; skipping blame",
                        base_sha[:12],
                        pr_number,
                    )
            except Exception:
                logger.debug("Blame fetch failed for PR #%d", pr_number, exc_info=True)

        # Fetch CVE-affected files from git history.
        cve_affected_files: list[str] | None = None
        if repo_path is not None and repo_path.is_dir():
            try:
                from franktheunicorn.scoring.cve_history import fetch_cve_affected_files

                cve_affected_files = fetch_cve_affected_files(
                    repo_path,
                    governance=project_config.governance,
                    extra_cve_files=project_config.cve_files,
                )
            except Exception:
                logger.debug("CVE history fetch failed for PR #%d", pr_number, exc_info=True)

        # Fetch all PR comments once: used for both mention scoring and
        # pending-response detection. The since filter is applied below only
        # for the author-reply subset.
        operator_review_posted_at: str | None = None
        author_replies: list[str] | None = None
        comment_bodies: list[str] | None = None

        try:
            all_comments = client.get_issue_comments(
                project_config.owner,
                project_config.repo,
                pr_number,
            )
            comment_bodies = [str(c.get("body", "")) for c in all_comments if c.get("body")]

            latest_posted_at = (
                ReviewDraft.objects.filter(
                    pull_request=pr_obj, status="posted", posted_at__isnull=False
                )
                .order_by("-posted_at")
                .values_list("posted_at", flat=True)
                .first()
            )
            if latest_posted_at is not None:
                operator_review_posted_at = latest_posted_at.isoformat()
                pr_author = pr_obj.author.lower()
                author_replies = [
                    c["created_at"]
                    for c in all_comments
                    if c.get("user", {}).get("login", "").lower() == pr_author
                    and c.get("created_at", "") >= operator_review_posted_at
                ]
        except Exception:
            logger.debug("Could not fetch comments for PR #%d", pr_number)

        # Score the PR
        score, breakdown = score_pull_request_from_model(
            pr=pr_obj,
            project_config=project_config,
            operator_username=operator_username,
            blame_data=blame_data,
            cve_affected_files=cve_affected_files,
            operator_review_posted_at=operator_review_posted_at,
            author_replies_after_review=author_replies,
            comment_bodies=comment_bodies,
        )
        pr_obj.interest_score = score
        pr_obj.score_breakdown = breakdown

        # Compute moderation flags and route to queue (§2.2).
        _route_pr_to_queue(
            pr_obj,
            operator_username,
            list(project.contributors_cache or []),
            _has_contributor_evidence(project),
            project_config,
        )

        pr_obj.save(
            update_fields=[
                "interest_score",
                "score_breakdown",
                "queue",
                "is_operator_pr",
                "is_new_contributor",
                "is_low_context",
                "is_likely_unowned",
                "mergeable",
                "base_sha",
                "head_sha",
                "head_branch",
            ]
        )

        results.append(pr_obj)

    _reroute_pull_requests(results, operator_username, project_config)

    queue_summary: dict[str, int] = {}
    for pr in results:
        queue_summary[pr.queue] = queue_summary.get(pr.queue, 0) + 1
    queue_str = ", ".join(f"{q}={n}" for q, n in sorted(queue_summary.items()))
    logger.info(
        "Polled %s: %d PRs ingested/updated (%s)",
        project.full_name,
        len(results),
        queue_str or "none",
    )
    return results


def _route_pr_to_queue(
    pr_obj: PullRequest,
    operator_username: str,
    forge_contributors: list[str] | None = None,
    contributor_evidence_available: bool = False,
    project_config: ProjectConfig | None = None,
) -> None:
    """Set queue and boolean flags based on moderation flags."""
    from franktheunicorn.scoring.moderation import compute_moderation_flags

    pr_dict: dict[str, object] = {
        "author": pr_obj.author,
        "title": pr_obj.title,
        "is_draft": pr_obj.is_draft,
        "additions": pr_obj.additions,
        "deletions": pr_obj.deletions,
        "body": pr_obj.body,
        "labels": pr_obj.labels,
        "changed_files": pr_obj.changed_files,
        "requested_reviewers": pr_obj.requested_reviewers,
    }
    if pr_obj.github_created_at:
        from datetime import UTC, datetime

        age = (datetime.now(tz=UTC) - pr_obj.github_created_at).days
        pr_dict["pr_age_days"] = age

    db_authors = list(
        PullRequest.objects.filter(project=pr_obj.project)
        .exclude(pk=pr_obj.pk)
        .values_list("author", flat=True)
        .distinct()
    )
    # Union DB authors with forge contributor list so known contributors aren't
    # incorrectly flagged as new when the local DB is sparse.
    known_authors = list({*db_authors, *(forge_contributors or [])})

    flags = compute_moderation_flags(
        pr_dict,
        operator_username,
        known_authors,
        contributor_evidence_available,
    )

    pr_obj.is_operator_pr = "is_operator_pr" in flags
    pr_obj.is_new_contributor = "new_contributor" in flags
    pr_obj.is_low_context = "low_context" in flags
    pr_obj.is_likely_unowned = "likely_unowned" in flags

    # When skip_wip is enabled and the PR is a draft or has a WIP title prefix,
    # park it in the "wip" queue — it will be re-routed when it graduates.
    skip_wip = project_config is not None and getattr(project_config, "skip_wip", False)
    is_wip = pr_obj.is_draft or "wip_title" in flags
    if skip_wip and is_wip:
        pr_obj.queue = "wip"
    # Route to queue based on priority of flags.
    elif pr_obj.is_operator_pr:
        pr_obj.queue = "your-prs"
    elif pr_obj.likely_ai_generated or "bot" in flags:
        pr_obj.queue = "ai-generated"
    elif pr_obj.is_new_contributor:
        pr_obj.queue = "new-contributor"
    elif pr_obj.is_likely_unowned:
        pr_obj.queue = "consider-closing"
    elif pr_obj.is_low_context:
        pr_obj.queue = "needs-triage"
    else:
        pr_obj.queue = "review"

    logger.debug(
        "PR #%d routed to queue=%r (flags=%s)",
        pr_obj.number,
        pr_obj.queue,
        ", ".join(flags) if flags else "none",
    )


def _has_contributor_evidence(project: Project) -> bool:
    """Return True when cached forge contributors can prove an author is absent."""
    return bool(project.contributors_cache)


def _refresh_contributors_cache(
    client: ForgeClient,
    project: Project,
    owner: str,
    repo: str,
) -> None:
    """Refresh contributor cache while preserving fetch status for routing."""
    try:
        forge_contributors = client.list_contributors(owner, repo)
    except Exception:
        project.contributors_cache_status = _CONTRIBUTORS_CACHE_FAILED
        project.save(update_fields=["contributors_cache_status"])
        logger.debug("Could not fetch contributors for %s/%s", owner, repo, exc_info=True)
        return

    if forge_contributors:
        project.contributors_cache = forge_contributors
        project.contributors_cache_status = _CONTRIBUTORS_CACHE_FETCHED
        project.save(update_fields=["contributors_cache", "contributors_cache_status"])
        logger.debug("Cached %d contributor(s) for %s/%s", len(forge_contributors), owner, repo)
        return

    project.contributors_cache = []
    project.contributors_cache_status = _CONTRIBUTORS_CACHE_EMPTY
    project.save(update_fields=["contributors_cache", "contributors_cache_status"])
    logger.debug("Contributor fetch for %s/%s returned no contributors", owner, repo)


def _reroute_pull_requests(
    pull_requests: list[PullRequest],
    operator_username: str,
    project_config: ProjectConfig,
) -> None:
    """Recompute moderation queues after a batch has fully populated local authors."""
    for pr_obj in pull_requests:
        before = (
            pr_obj.queue,
            pr_obj.is_operator_pr,
            pr_obj.is_new_contributor,
            pr_obj.is_low_context,
            pr_obj.is_likely_unowned,
        )
        _route_pr_to_queue(
            pr_obj,
            operator_username,
            list(pr_obj.project.contributors_cache or []),
            _has_contributor_evidence(pr_obj.project),
            project_config,
        )
        after = (
            pr_obj.queue,
            pr_obj.is_operator_pr,
            pr_obj.is_new_contributor,
            pr_obj.is_low_context,
            pr_obj.is_likely_unowned,
        )
        if after != before:
            pr_obj.save(
                update_fields=[
                    "queue",
                    "is_operator_pr",
                    "is_new_contributor",
                    "is_low_context",
                    "is_likely_unowned",
                ]
            )


def _parse_github_datetime(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


@transaction.atomic
def _upsert_pull_request(
    project: Project,
    pr_data: dict[str, Any],
    changed_files: list[str] | None,
) -> PullRequest:
    """Create or update a PullRequest from raw GitHub API data.

    Handles two degraded-data cases so a rate-limit window doesn't gut
    already-ingested PRs:

    - ``pr_data["_scraped"]`` marks an HTML-scrape fallback record whose
      body/labels/additions/timestamps are placeholders. On an *existing*
      row those fields are preserved; only number/title/author/state/url
      (which the scrape reliably provides) are refreshed.
    - ``changed_files is None`` means the files fetch failed; the existing
      ``changed_files`` is preserved rather than overwritten with ``[]``.
    """
    pr_number: int = pr_data["number"]
    user_data = pr_data.get("user") or {}
    is_scraped = bool(pr_data.get("_scraped"))

    existing = PullRequest.objects.filter(project=project, number=pr_number).first()

    # Fields the scrape path provides reliably — always safe to refresh.
    defaults: dict[str, Any] = {
        "title": pr_data.get("title", ""),
        "author": user_data.get("login", "unknown"),
        "state": pr_data.get("state", "open"),
        "url": pr_data.get("html_url", ""),
        "diff_url": pr_data.get("diff_url", ""),
    }

    # Rich fields only the API provides. On a degraded scrape record for an
    # *existing* row, skip them entirely so we don't null out good data; a
    # brand-new PR (even one seen only via scrape) still gets seeded.
    if is_scraped and existing is not None:
        pass
    else:
        github_id = pr_data.get("id", 0)
        # Never overwrite a real stored id with a scrape's 0.
        if not github_id and existing is not None:
            github_id = existing.github_id
        defaults["github_id"] = github_id
        body = pr_data.get("body", "") or ""
        defaults.update(
            {
                "body": body,
                "labels": [lbl.get("name", "") for lbl in pr_data.get("labels", [])],
                "requested_reviewers": [
                    r.get("login", "") for r in pr_data.get("requested_reviewers", [])
                ],
                "assignees": [a.get("login", "") for a in pr_data.get("assignees", [])],
                "additions": pr_data.get("additions", 0),
                "deletions": pr_data.get("deletions", 0),
                "is_draft": pr_data.get("draft", False),
                "github_created_at": _parse_github_datetime(pr_data.get("created_at")),
                "github_updated_at": _parse_github_datetime(pr_data.get("updated_at")),
            }
        )

        # Detect AI agent session from PR description (v1.25). Always set
        # these fields so stale values are cleared on update — including the
        # boolean, otherwise a PR whose author edited the session link away
        # stays latched in the ai-generated queue forever.
        session = detect_agent_session(body) if body else None
        defaults["ai_agent_source"] = session.agent_source if session else ""
        defaults["agent_session_url"] = session.session_url if session else ""
        defaults["agent_task_id"] = session.task_id if session else ""
        defaults["likely_ai_generated"] = session is not None

    # changed_files: None means "fetch failed, keep what we have".
    if changed_files is not None:
        defaults["changed_files"] = changed_files
    elif existing is None:
        defaults["changed_files"] = []

    pr_obj, _created = PullRequest.objects.update_or_create(
        project=project,
        number=pr_number,
        defaults=defaults,
    )
    return pr_obj


def ingest_single_pr(owner: str, repo: str, pr_number: int) -> PullRequest:
    """Fetch a single PR from the forge, score it, and store it in the DB.

    Creates the Project row if it doesn't exist yet. Safe to call repeatedly —
    uses update_or_create internally so it acts as a refresh when the PR is
    already in the DB.
    """
    from franktheunicorn.backends import make_client
    from franktheunicorn.config.loader import get_operator_config, get_project_config
    from franktheunicorn.config.resolver import get_forge_entry

    operator_config = get_operator_config()
    project_config = get_project_config(f"{owner}/{repo}")
    forge_name = getattr(project_config, "forge", None) or "github"
    entry = get_forge_entry(operator_config, forge_name)
    client = make_client(entry)

    project, _ = Project.objects.update_or_create(
        owner=owner,
        repo=repo,
        defaults={"review_context": getattr(project_config, "review_context", "") or ""},
    )

    pr_data = client.get_pull_request(owner, repo, pr_number)
    changed_files: list[str] | None
    try:
        files_data = client.get_pull_request_files(owner, repo, pr_number)
        changed_files = [f["filename"] for f in files_data]
    except Exception:
        logger.warning("Could not fetch files for PR #%d during on-demand ingest", pr_number)
        changed_files = None

    pr_obj = _upsert_pull_request(project, pr_data, changed_files)

    if project_config:
        score, breakdown = score_pull_request_from_model(
            pr=pr_obj,
            project_config=project_config,
            operator_username=operator_config.github_username or "",
        )
        pr_obj.interest_score = score
        pr_obj.score_breakdown = breakdown

    # Hydrate contributors_cache if missing so new-contributor detection isn't
    # limited to DB-only PR authors (which are sparse on first/force ingest).
    if not project.contributors_cache:
        _refresh_contributors_cache(client, project, owner, repo)

    _route_pr_to_queue(
        pr_obj,
        operator_config.github_username or "",
        list(project.contributors_cache or []),
        _has_contributor_evidence(project),
        project_config if isinstance(project_config, ProjectConfig) else None,
    )

    pr_obj.save(
        update_fields=[
            "interest_score",
            "score_breakdown",
            "queue",
            "is_operator_pr",
            "is_new_contributor",
            "is_low_context",
            "is_likely_unowned",
        ]
    )
    return pr_obj
