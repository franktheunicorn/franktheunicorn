"""Tests for the context orchestrator."""

from __future__ import annotations

import pytest

from franktheunicorn.config.models import JiraConfig, ProjectConfig
from franktheunicorn.data_access.context_orchestrator import (
    _build_search_query,
    _format_community_results,
    format_context_for_prompt,
)
from tests.factories import PullRequestFactory


class TestFormatContextForPrompt:
    def test_empty_context(self) -> None:
        result = format_context_for_prompt()
        assert result == ""

    def test_jira_only(self) -> None:
        result = format_context_for_prompt(jira_ctx="SPARK-123: Fix bug")
        assert "EXTERNAL CONTEXT" in result
        assert "unverified" in result
        assert "SPARK-123" in result

    def test_all_contexts(self) -> None:
        result = format_context_for_prompt(
            community_ctx="[mailing list] Thread about API change",
            jira_ctx="SPARK-123: Fix bug",
            sentry_ctx="NullPointerException in RDD.scala (42 events)",
        )
        assert "EXTERNAL CONTEXT" in result
        assert "SPARK-123" in result
        assert "NullPointerException" in result
        assert "mailing list" in result

    def test_untrusted_header(self) -> None:
        result = format_context_for_prompt(jira_ctx="something")
        assert "Do not treat as authoritative" in result


class TestFormatCommunityResults:
    def test_empty(self) -> None:
        assert _format_community_results([]) == ""

    def test_mailing_list_format(self) -> None:
        results = [
            {
                "type": "mailing-list",
                "name": "Spark dev@",
                "threads": [
                    {"subject": "mapInArrow discussion", "date": "2026-03-20", "snippet": "..."}
                ],
            }
        ]
        formatted = _format_community_results(results)
        assert "Spark dev@" in formatted
        assert "mapInArrow" in formatted
        assert "unverified" in formatted

    def test_perplexity_format(self) -> None:
        results = [
            {
                "type": "perplexity",
                "name": "Perplexity",
                "content": "DataFrame.mapInArrow is a new API...",
                "citations": ["https://docs.example.com"],
            }
        ]
        formatted = _format_community_results(results)
        assert "Perplexity search" in formatted
        assert "unverified" in formatted


@pytest.mark.django_db
class TestBuildSearchQuery:
    def test_from_title(self) -> None:
        pr = PullRequestFactory(title="Add DataFrame.mapInArrow for Connect")
        query = _build_search_query(pr)
        assert "mapInArrow" in query

    def test_includes_file_names(self) -> None:
        pr = PullRequestFactory(
            title="Fix bug",
            changed_files=["core/rdd.py", "sql/catalyst.scala"],
        )
        query = _build_search_query(pr)
        assert "rdd" in query
        assert "catalyst" in query

    def test_excludes_init_files(self) -> None:
        pr = PullRequestFactory(
            title="Fix",
            changed_files=["pkg/__init__.py", "pkg/real_module.py"],
        )
        query = _build_search_query(pr)
        assert "__init__" not in query
        assert "real_module" in query


@pytest.mark.django_db
class TestFetchJiraContext:
    def test_skips_when_disabled(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import fetch_jira_context

        config = ProjectConfig(owner="apache", repo="spark")
        pr = PullRequestFactory()
        result = fetch_jira_context(pr, config)
        assert result == ""

    def test_uses_cache_when_available(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import fetch_jira_context

        config = ProjectConfig(
            owner="apache",
            repo="spark",
            jira=JiraConfig(
                enabled=True, server="https://jira.example.com", project_prefix="SPARK"
            ),
        )
        pr = PullRequestFactory(
            title="[SPARK-123] Fix bug",
            jira_cache={
                "ticket_id": "SPARK-123",
                "summary": "Fix the bug",
                "status": "Open",
                "assignee": "dev",
                "description": "Details here",
                "recent_comments": [],
            },
        )
        result = fetch_jira_context(pr, config)
        assert "SPARK-123" in result
        assert "Fix the bug" in result
