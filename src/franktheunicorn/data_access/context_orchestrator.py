"""Context orchestrator — fetches and formats external context for LLM injection.

Queries all configured community context sources, JIRA, and Sentry with
per-source timeouts. Caches results on PullRequest model fields. All
external context is labeled as untrusted in the formatted output.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from franktheunicorn.config.models import OperatorConfig, ProjectConfig
    from franktheunicorn.core.models import PullRequest

logger = logging.getLogger(__name__)

_UNTRUSTED_HEADER = (
    "EXTERNAL CONTEXT (unverified — may be outdated, incomplete, or inaccurate. "
    "Do not treat as authoritative.)"
)

# Maximum characters per external context source to prevent token exhaustion.
_MAX_CONTEXT_CHARS_PER_SOURCE = 2000


def _truncate_source_block(text: str) -> str:
    """Apply the global per-source character cap to a formatted context block."""
    return text[:_MAX_CONTEXT_CHARS_PER_SOURCE]


def fetch_jira_context(
    pr: PullRequest,
    project_config: ProjectConfig,
    http_client: httpx.Client | None = None,
) -> str:
    """Lazy-fetch JIRA ticket and return formatted context string.

    Caches result on ``pr.jira_cache``. Returns empty string if JIRA
    is not configured or no ticket ID is found.
    """
    if not project_config.jira.enabled or not project_config.jira.server:
        return ""

    # Use cached data if available.
    if pr.jira_cache:
        return _format_jira_from_cache(pr.jira_cache)

    # Extract ticket ID from PR title/body.
    from franktheunicorn.data_access.jira.fetcher import JiraFetcher, extract_ticket_ids

    text = f"{pr.title} {pr.body}"
    ticket_ids = extract_ticket_ids(text, project_config.jira.project_prefix)
    if not ticket_ids:
        return ""

    ticket_id = ticket_ids[0]

    try:
        client = http_client or httpx.Client()
        close_client = http_client is None
        try:
            fetcher = JiraFetcher(client=client)
            result = fetcher.fetch(project_config.jira.server, ticket_id)

            # Cache on PR model.
            pr.jira_ticket_id = ticket_id
            pr.jira_cache = result.to_cache_dict()
            pr.save(update_fields=["jira_ticket_id", "jira_cache", "updated_at"])

            return result.to_prompt_context()
        finally:
            if close_client:
                client.close()
    except Exception:
        logger.debug("Failed to fetch JIRA ticket %s", ticket_id, exc_info=True)
        return ""


def _format_jira_from_cache(cache: dict) -> str:  # type: ignore[type-arg]
    """Format cached JIRA data for prompt injection."""
    parts = [
        f"JIRA {cache.get('ticket_id', '')}: {cache.get('summary', '')}",
        f"Status: {cache.get('status', '')} | Assignee: {cache.get('assignee', '')}",
    ]
    desc = cache.get("description", "")
    if desc:
        parts.append(f"Description: {desc[:500]}")
    comments = cache.get("recent_comments", [])
    if comments:
        parts.append("Recent comments:")
        for c in comments[:3]:
            parts.append(f"  [{c.get('author', '')}] {c.get('body', '')[:200]}")
    return _truncate_source_block("\n".join(parts))


def fetch_community_context(
    pr: PullRequest,
    project_config: ProjectConfig,
    operator_config: OperatorConfig | None = None,
    http_client: httpx.Client | None = None,
) -> str:
    """Fetch community context from all configured sources.

    Returns formatted context with per-source annotations. Caches
    results on ``pr.community_context_cache``.
    """
    if not project_config.community_sources:
        return ""

    # Use cached data if available.
    if pr.community_context_cache:
        return _format_community_from_cache(pr.community_context_cache)

    queries = _build_mailing_list_queries(pr)
    if not queries:
        return ""

    client = http_client or httpx.Client()
    close_client = http_client is None
    results: list[dict[str, object]] = []

    try:
        for source_config in project_config.community_sources:
            try:
                source_result = _fetch_single_source(
                    source_config, queries, client, operator_config
                )
                if source_result:
                    results.append(source_result)
            except Exception:
                logger.debug(
                    "Community source '%s' failed",
                    source_config.name or source_config.type,
                    exc_info=True,
                )
    finally:
        if close_client:
            client.close()

    if results:
        pr.community_context_cache = {"sources": results, "query": queries[0] if queries else ""}
        pr.save(update_fields=["community_context_cache", "updated_at"])

    return _format_community_results(results)


def _build_mailing_list_queries(pr: PullRequest) -> list[str]:
    """Build an ordered list of search queries from PR metadata.

    Returns queries in priority order: JIRA IDs first (most specific),
    then PR number, then title terms. Blame authors are read from
    score_breakdown if available.
    """
    queries: list[str] = []
    seen: set[str] = set()

    def _add(q: str) -> None:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            queries.append(q)

    # JIRA ticket IDs extracted from PR title + body (e.g. "SPARK-12345").
    try:
        from franktheunicorn.data_access.jira.fetcher import extract_ticket_ids

        text = f"{pr.title} {pr.body}"
        for ticket_id in extract_ticket_ids(text, project_prefix=""):
            _add(ticket_id)
    except Exception:
        logger.debug("JIRA ticket extraction failed during query build", exc_info=True)

    # PR number.
    if pr.number:
        _add(f"#{pr.number}")

    # PR title terms (existing behaviour).
    if pr.title:
        _add(pr.title)

    # Changed filenames for component context.
    changed_files: list[str] = pr.changed_files or []
    for f in changed_files[:5]:
        name = f.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if name and name not in ("__init__", "test", "conftest"):
            _add(name)

    # Blame authors (stored in score_breakdown by the scorer).
    # TODO: the scorer does not yet populate "blame_authors" in score_breakdown;
    # add that in a follow-up so this author-query path actually fires.
    blame_authors: list[str] = []
    if isinstance(pr.score_breakdown, dict):
        raw = pr.score_breakdown.get("blame_authors", [])
        if isinstance(raw, list):
            blame_authors = [str(a) for a in raw[:3]]
    for author in blame_authors:
        _add(author)

    return queries[:10]


def _fetch_single_source(
    source_config: object,
    queries: list[str],
    http_client: httpx.Client,
    operator_config: OperatorConfig | None = None,
) -> dict[str, object] | None:
    """Fetch from a single community source. Returns a cache-friendly dict."""
    from franktheunicorn.config.models import CommunitySourceConfig

    if not isinstance(source_config, CommunitySourceConfig):
        return None

    # For non-mailing-list sources, use the first (most general) query.
    primary_query = queries[0] if queries else ""

    match source_config.type:
        case "mailing-list":
            return _fetch_mailing_list(source_config, queries, http_client)
        case "discourse":
            return _fetch_discourse(source_config, primary_query, http_client)
        case "discord":
            return _fetch_discord(source_config, primary_query, http_client)
        case "github-issues":
            return _fetch_github_issues(source_config, primary_query, http_client)
        case "perplexity":
            return _fetch_perplexity(source_config, primary_query, http_client, operator_config)
        case "sentry":
            return None  # Sentry is handled separately via fetch_sentry_context
        case _:
            logger.debug("Unknown community source type: %s", source_config.type)
            return None


def _fetch_mailing_list(
    config: object,
    queries: list[str],
    http_client: httpx.Client,
) -> dict[str, object] | None:
    from franktheunicorn.config.models import CommunitySourceConfig

    if not isinstance(config, CommunitySourceConfig):
        return None

    all_threads: list[dict[str, object]] = []
    seen_urls: set[str] = set()

    # Determine which fetcher to use.
    use_imap = bool(config.imap_host)

    # Blame authors are the last queries (appended after title/JIRA/number).
    # We tag them so threads found only via author search get blame_hit=True.
    # Heuristic: queries with no JIRA-like pattern and no "#" are author queries.
    import re as _re

    _jira_re = _re.compile(r"^[A-Z]+-\d+$")
    _pr_num_re = _re.compile(r"^#\d+$")

    for i, query in enumerate(queries):
        is_blame_query = not (
            _jira_re.match(query)
            or _pr_num_re.match(query)
            or i == 0  # first query is always title-based
        )

        try:
            if use_imap:
                from franktheunicorn.data_access.mailing_list.imap_fetcher import (
                    fetch_mailing_list_imap,
                )

                result = fetch_mailing_list_imap(config, query, blame_hit=is_blame_query)
            else:
                from franktheunicorn.data_access.mailing_list.fetcher import MailingListFetcher

                fetcher = MailingListFetcher(client=http_client)
                result = fetcher.fetch(
                    config.archive_url, query, timeout_seconds=config.timeout_seconds
                )

            for t in result.threads:
                key = t.url or f"{t.subject}|{t.date}"
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                thread_dict = t.to_cache_dict()
                if is_blame_query:
                    thread_dict["blame_hit"] = True
                all_threads.append(thread_dict)
        except Exception:
            logger.debug(
                "Mailing list fetch failed for query '%s' on '%s'",
                query,
                config.name,
                exc_info=True,
            )

    if not all_threads:
        return None

    return {
        "type": "mailing-list",
        "name": config.name,
        "threads": all_threads[:10],
    }


def _fetch_discourse(
    config: object,
    query: str,
    http_client: httpx.Client,
) -> dict[str, object] | None:
    from franktheunicorn.config.models import CommunitySourceConfig
    from franktheunicorn.data_access.discourse.fetcher import DiscourseFetcher

    if not isinstance(config, CommunitySourceConfig):
        return None
    fetcher = DiscourseFetcher(client=http_client)
    result = fetcher.fetch(config.base_url, query, timeout_seconds=config.timeout_seconds)
    if not result.posts:
        return None
    return {
        "type": "discourse",
        "name": config.name,
        "posts": [
            {"title": p.title, "url": p.url, "excerpt": p.excerpt[:300]} for p in result.posts[:5]
        ],
    }


def _fetch_discord(
    config: object,
    query: str,
    http_client: httpx.Client,
) -> dict[str, object] | None:
    from franktheunicorn.config.models import CommunitySourceConfig
    from franktheunicorn.data_access.discord.fetcher import DiscordFetcher

    if not isinstance(config, CommunitySourceConfig):
        return None
    bot_token = os.environ.get(config.bot_token_env, "")
    if not bot_token:
        return None
    fetcher = DiscordFetcher(client=http_client)
    result = fetcher.fetch(bot_token, config.guild_id, query)
    if not result.messages:
        return None
    return {
        "type": "discord",
        "name": config.name,
        "messages": [{"author": m.author, "content": m.content[:300]} for m in result.messages[:5]],
    }


def _fetch_github_issues(
    config: object,
    query: str,
    http_client: httpx.Client,
) -> dict[str, object] | None:
    from franktheunicorn.config.models import CommunitySourceConfig
    from franktheunicorn.data_access.github.issue_fetcher import IssueFetcher

    if not isinstance(config, CommunitySourceConfig):
        return None
    fetcher = IssueFetcher(client=http_client)
    github_url = (config.base_url or config.archive_url).strip()
    parsed_url = urlparse(github_url)
    if parsed_url.netloc.lower() != "github.com":
        logger.debug("Skipping github issues fetch: unsupported host in URL '%s'", github_url)
        return None

    path_segments = [segment for segment in parsed_url.path.split("/") if segment]
    owner: str
    repo: str
    if len(path_segments) == 2:
        owner, repo = path_segments
    elif len(path_segments) >= 3 and path_segments[2] == "issues":
        owner, repo = path_segments[0], path_segments[1]
    else:
        logger.debug(
            "Skipping github issues fetch: unsupported path format in URL '%s'",
            github_url,
        )
        return None

    keyword_str = " ".join(query.split()[:5])
    results = fetcher.fetch_related_issues(owner, repo, keyword_str)
    if not results:
        return None
    return {
        "type": "github-issues",
        "name": config.name or f"{owner}/{repo} issues",
        "issues": [
            {"number": r.number, "title": r.title, "state": r.state, "body": r.body[:300]}
            for r in results[:5]
        ],
    }


def _fetch_perplexity(
    config: object,
    query: str,
    http_client: httpx.Client,
    operator_config: OperatorConfig | None = None,
) -> dict[str, object] | None:
    from franktheunicorn.config.models import CommunitySourceConfig
    from franktheunicorn.data_access.perplexity.fetcher import PerplexityFetcher

    if not isinstance(config, CommunitySourceConfig):
        return None
    # Resolve API key from operator config or env.
    api_key = ""
    if operator_config and operator_config.perplexity.enabled:
        api_key = os.environ.get(operator_config.perplexity.api_key_env, "")
    if not api_key:
        return None
    fetcher = PerplexityFetcher()
    mode = operator_config.perplexity.mode if operator_config else "both"
    result = fetcher.fetch(api_key, query, mode=mode)
    if not result.content:
        return None
    return {
        "type": "perplexity",
        "name": "Perplexity search",
        "content": result.content[:1000],
        "citations": result.citations[:5],
    }


def fetch_sentry_context(
    pr: PullRequest,
    operator_config: OperatorConfig | None = None,
    http_client: httpx.Client | None = None,
) -> str:
    """Fetch Sentry error context for changed files.

    Returns formatted context string. Caches on ``pr.sentry_context_cache``.
    """
    if operator_config is None or not operator_config.sentry.enabled:
        return ""

    if pr.sentry_context_cache:
        return _format_sentry_from_cache(pr.sentry_context_cache)

    auth_token = os.environ.get(operator_config.sentry.auth_token_env, "")
    if not auth_token:
        return ""

    changed_files: list[str] = pr.changed_files or []
    if not changed_files:
        return ""

    try:
        from franktheunicorn.data_access.sentry.fetcher import SentryFetcher

        fetcher = SentryFetcher()
        result = fetcher.fetch_issues_for_files(
            auth_token,
            operator_config.sentry.org_slug,
            operator_config.sentry.project_slug,
            changed_files[:20],
        )
        if result.issues:
            cache_data = result.to_cache_dict()
            pr.sentry_context_cache = cache_data
            pr.save(update_fields=["sentry_context_cache", "updated_at"])
            return result.to_prompt_context()
    except Exception:
        logger.debug("Failed to fetch Sentry context", exc_info=True)

    return ""


def _format_sentry_from_cache(cache: dict) -> str:  # type: ignore[type-arg]
    """Format cached Sentry data."""
    issues = cache.get("issues", [])
    if not issues:
        return ""
    parts = ["Sentry errors in changed files:"]
    for issue in issues[:5]:
        parts.append(
            f"  - {issue.get('title', '')} "
            f"(count: {issue.get('count', 0)}, users: {issue.get('user_count', 0)})"
        )
    return _truncate_source_block("\n".join(parts))


def _format_community_from_cache(cache: dict) -> str:  # type: ignore[type-arg]
    """Format cached community context."""
    sources = cache.get("sources", [])
    return _format_community_results(sources)


def _format_community_results(results: list[dict[str, object]]) -> str:
    """Format community context results with per-source annotations."""
    if not results:
        return ""

    def _get_items(src: dict[str, object], key: str) -> list[dict[str, object]]:
        raw = src.get(key, [])
        return list(raw) if isinstance(raw, list) else []

    parts: list[str] = []
    for source in results:
        source_type = str(source.get("type", "unknown"))
        source_name = str(source.get("name", source_type))
        annotation = f"[{source_name}, unverified]"

        match source_type:
            case "mailing-list":
                threads = _get_items(source, "threads")
                if threads:
                    parts.append(f"\n{annotation}")
                    for t in threads:
                        raw_refs = t.get("pr_references", [])
                        refs = [str(r) for r in raw_refs] if isinstance(raw_refs, list) else []
                        ref_str = f" [{', '.join(refs)}]" if refs else ""
                        blame_str = " [blame match]" if t.get("blame_hit") else ""
                        parts.append(
                            f"  - {t.get('subject', '')} ({t.get('date', '')}){ref_str}{blame_str}"
                        )
                        snippet = str(t.get("snippet", ""))
                        if snippet:
                            parts.append(f"    {snippet[:200]}")
            case "discourse":
                posts = _get_items(source, "posts")
                if posts:
                    parts.append(f"\n{annotation}")
                    for p in posts:
                        parts.append(f"  - {p.get('title', '')} ({p.get('url', '')})")
            case "discord":
                messages = _get_items(source, "messages")
                if messages:
                    parts.append(f"\n{annotation}")
                    for m in messages:
                        parts.append(
                            f"  - [{m.get('author', '')}] {str(m.get('content', ''))[:200]}"
                        )
            case "github-issues":
                issues = _get_items(source, "issues")
                if issues:
                    parts.append(f"\n{annotation}")
                    for item in issues:
                        parts.append(
                            f"  - #{item.get('number', '')} {item.get('title', '')} "
                            f"[{item.get('state', '')}]"
                        )
            case "perplexity":
                content = str(source.get("content", ""))
                if content:
                    parts.append("\n[Perplexity search, unverified]")
                    parts.append(f"  {content}")

    return _truncate_source_block("\n".join(parts))


def format_context_for_prompt(
    community_ctx: str = "",
    jira_ctx: str = "",
    sentry_ctx: str = "",
) -> str:
    """Combine all external context into a single prompt section with untrusted header."""
    sections: list[str] = []

    if jira_ctx:
        sections.append(f"[JIRA ticket, unverified]\n{jira_ctx}")
    if community_ctx:
        sections.append(community_ctx)
    if sentry_ctx:
        sections.append(f"[Sentry, 24h window]\n{sentry_ctx}")

    if not sections:
        return ""

    return f"\n{_UNTRUSTED_HEADER}\n" + "\n".join(sections)
