"""Tests for the context orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import (
    CommunitySourceConfig,
    JiraConfig,
    OperatorConfig,
    PerplexityConfig,
    ProjectConfig,
    SentryConfig,
)
from franktheunicorn.data_access.context_orchestrator import (
    _build_search_query,
    _format_community_from_cache,
    _format_community_results,
    _format_sentry_from_cache,
    fetch_community_context,
    fetch_sentry_context,
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

    def test_uses_cache_with_comments(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import fetch_jira_context

        config = ProjectConfig(
            owner="apache",
            repo="spark",
            jira=JiraConfig(
                enabled=True, server="https://jira.example.com", project_prefix="SPARK"
            ),
        )
        pr = PullRequestFactory(
            title="[SPARK-456] Add feature",
            jira_cache={
                "ticket_id": "SPARK-456",
                "summary": "Add the feature",
                "status": "In Progress",
                "assignee": "dev",
                "description": "Feature details",
                "recent_comments": [
                    {"author": "reviewer", "body": "Looks good to me"},
                    {"author": "dev", "body": "Updated the PR"},
                ],
            },
        )
        result = fetch_jira_context(pr, config)
        assert "SPARK-456" in result
        assert "Recent comments" in result
        assert "reviewer" in result
        assert "Looks good" in result

    @patch("franktheunicorn.data_access.jira.fetcher.JiraFetcher")
    @patch("franktheunicorn.data_access.jira.fetcher.extract_ticket_ids")
    def test_fetches_and_caches_jira(
        self, mock_extract: MagicMock, mock_fetcher_cls: MagicMock
    ) -> None:
        from franktheunicorn.data_access.context_orchestrator import fetch_jira_context

        mock_extract.return_value = ["SPARK-789"]

        mock_result = MagicMock()
        mock_result.to_cache_dict.return_value = {
            "ticket_id": "SPARK-789",
            "summary": "A ticket",
            "status": "Open",
            "assignee": "dev",
            "description": "",
            "recent_comments": [],
        }
        mock_result.to_prompt_context.return_value = "SPARK-789: A ticket"
        mock_fetcher_cls.return_value.fetch.return_value = mock_result

        config = ProjectConfig(
            owner="apache",
            repo="spark",
            jira=JiraConfig(
                enabled=True, server="https://jira.example.com", project_prefix="SPARK"
            ),
        )
        pr = PullRequestFactory(title="[SPARK-789] Do thing", body="Details")
        client = MagicMock()
        result = fetch_jira_context(pr, config, http_client=client)
        assert "SPARK-789" in result
        pr.refresh_from_db()
        assert pr.jira_cache is not None

    @patch("franktheunicorn.data_access.jira.fetcher.extract_ticket_ids")
    def test_returns_empty_when_no_ticket_ids(self, mock_extract: MagicMock) -> None:
        from franktheunicorn.data_access.context_orchestrator import fetch_jira_context

        mock_extract.return_value = []

        config = ProjectConfig(
            owner="apache",
            repo="spark",
            jira=JiraConfig(
                enabled=True, server="https://jira.example.com", project_prefix="SPARK"
            ),
        )
        pr = PullRequestFactory(title="No ticket here")
        result = fetch_jira_context(pr, config)
        assert result == ""

    @patch("franktheunicorn.data_access.jira.fetcher.JiraFetcher")
    @patch("franktheunicorn.data_access.jira.fetcher.extract_ticket_ids")
    def test_handles_jira_exception(
        self, mock_extract: MagicMock, mock_fetcher_cls: MagicMock
    ) -> None:
        from franktheunicorn.data_access.context_orchestrator import fetch_jira_context

        mock_extract.return_value = ["SPARK-999"]
        mock_fetcher_cls.return_value.fetch.side_effect = RuntimeError("connection failed")

        config = ProjectConfig(
            owner="apache",
            repo="spark",
            jira=JiraConfig(
                enabled=True, server="https://jira.example.com", project_prefix="SPARK"
            ),
        )
        pr = PullRequestFactory(title="[SPARK-999] Broken")
        result = fetch_jira_context(pr, config)
        assert result == ""


class TestFormatSentryFromCache:
    def test_empty_issues(self) -> None:
        assert _format_sentry_from_cache({"issues": []}) == ""

    def test_no_issues_key(self) -> None:
        assert _format_sentry_from_cache({}) == ""

    def test_formats_issues(self) -> None:
        cache = {
            "issues": [
                {"title": "NullPointerException", "count": 42, "user_count": 10},
                {"title": "TimeoutError", "count": 5, "user_count": 2},
            ]
        }
        result = _format_sentry_from_cache(cache)
        assert "Sentry errors" in result
        assert "NullPointerException" in result
        assert "42" in result
        assert "TimeoutError" in result


class TestFormatCommunityFromCache:
    def test_empty_cache(self) -> None:
        assert _format_community_from_cache({"sources": []}) == ""

    def test_delegates_to_format_results(self) -> None:
        cache = {
            "sources": [
                {
                    "type": "discourse",
                    "name": "Spark Forum",
                    "posts": [{"title": "Arrow API", "url": "https://forum.example.com/t/1"}],
                }
            ]
        }
        result = _format_community_from_cache(cache)
        assert "Spark Forum" in result
        assert "Arrow API" in result


class TestFormatCommunityResultsAllTypes:
    """Cover all source type branches in _format_community_results."""

    def test_discourse_format(self) -> None:
        results = [
            {
                "type": "discourse",
                "name": "Spark Forum",
                "posts": [
                    {"title": "Arrow UDF Discussion", "url": "https://forum.example.com/t/42"}
                ],
            }
        ]
        formatted = _format_community_results(results)
        assert "Spark Forum" in formatted
        assert "Arrow UDF Discussion" in formatted
        assert "https://forum.example.com/t/42" in formatted
        assert "unverified" in formatted

    def test_discord_format(self) -> None:
        results = [
            {
                "type": "discord",
                "name": "Spark Discord",
                "messages": [{"author": "dev_user", "content": "Check the new Arrow API"}],
            }
        ]
        formatted = _format_community_results(results)
        assert "Spark Discord" in formatted
        assert "dev_user" in formatted
        assert "Check the new Arrow API" in formatted

    def test_github_issues_format(self) -> None:
        results = [
            {
                "type": "github-issues",
                "name": "apache/spark issues",
                "issues": [
                    {"number": 123, "title": "Fix Arrow API", "state": "open"},
                    {"number": 456, "title": "Improve performance", "state": "closed"},
                ],
            }
        ]
        formatted = _format_community_results(results)
        assert "#123" in formatted
        assert "Fix Arrow API" in formatted
        assert "[open]" in formatted
        assert "#456" in formatted

    def test_multiple_sources(self) -> None:
        results = [
            {
                "type": "mailing-list",
                "name": "dev@spark",
                "threads": [{"subject": "Thread A", "date": "2026-01-01", "snippet": "snip"}],
            },
            {
                "type": "perplexity",
                "name": "Perplexity search",
                "content": "Some search result",
                "citations": [],
            },
        ]
        formatted = _format_community_results(results)
        assert "dev@spark" in formatted
        assert "Perplexity search" in formatted

    def test_unknown_type_ignored(self) -> None:
        results = [{"type": "unknown-source", "name": "mystery"}]
        formatted = _format_community_results(results)
        assert formatted == ""


@pytest.mark.django_db
class TestBuildSearchQueryEdgeCases:
    def test_empty_title_and_no_files(self) -> None:
        pr = PullRequestFactory(title="", changed_files=[])
        query = _build_search_query(pr)
        assert query == ""

    def test_excludes_test_and_conftest(self) -> None:
        pr = PullRequestFactory(
            title="Update",
            changed_files=["tests/test.py", "tests/conftest.py", "src/module.py"],
        )
        query = _build_search_query(pr)
        assert "test" not in query.split()
        assert "conftest" not in query.split()
        assert "module" in query

    def test_limits_file_parts_to_five(self) -> None:
        pr = PullRequestFactory(
            title="Fix",
            changed_files=[
                "a/mod1.py",
                "b/mod2.py",
                "c/mod3.py",
                "d/mod4.py",
                "e/mod5.py",
                "f/mod6.py",
                "g/mod7.py",
            ],
        )
        query = _build_search_query(pr)
        # Title contributes 1 part, only first 5 files are used
        parts = query.split()
        # Should have title + at most 5 file names
        assert len(parts) <= 6


@pytest.mark.django_db
class TestFetchMailingList:
    @patch("franktheunicorn.data_access.mailing_list.fetcher.MailingListFetcher")
    def test_returns_threads(self, mock_fetcher_cls: MagicMock) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_mailing_list

        thread = MagicMock()
        thread.subject = "Arrow discussion"
        thread.date = "2026-03-20"
        thread.snippet = "Some snippet about Arrow"

        mock_result = MagicMock()
        mock_result.threads = [thread]
        mock_fetcher_cls.return_value.fetch.return_value = mock_result

        config = CommunitySourceConfig(
            type="mailing-list",
            name="dev@spark",
            archive_url="https://lists.apache.org/dev@spark",
        )
        result = _fetch_mailing_list(config, "Arrow API", MagicMock())
        assert result is not None
        assert result["type"] == "mailing-list"
        assert result["name"] == "dev@spark"
        assert len(result["threads"]) == 1

    @patch("franktheunicorn.data_access.mailing_list.fetcher.MailingListFetcher")
    def test_returns_none_when_no_threads(self, mock_fetcher_cls: MagicMock) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_mailing_list

        mock_result = MagicMock()
        mock_result.threads = []
        mock_fetcher_cls.return_value.fetch.return_value = mock_result

        config = CommunitySourceConfig(
            type="mailing-list", name="dev@spark", archive_url="https://lists.example.com"
        )
        result = _fetch_mailing_list(config, "query", MagicMock())
        assert result is None

    def test_returns_none_for_wrong_config_type(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_mailing_list

        result = _fetch_mailing_list("not a config", "query", MagicMock())
        assert result is None


@pytest.mark.django_db
class TestFetchDiscourse:
    @patch("franktheunicorn.data_access.discourse.fetcher.DiscourseFetcher")
    def test_returns_posts(self, mock_fetcher_cls: MagicMock) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_discourse

        post = MagicMock()
        post.title = "Arrow UDF topic"
        post.url = "https://discourse.example.com/t/42"
        post.excerpt = "Discussion about Arrow UDFs"

        mock_result = MagicMock()
        mock_result.posts = [post]
        mock_fetcher_cls.return_value.fetch.return_value = mock_result

        config = CommunitySourceConfig(
            type="discourse", name="Spark Forum", base_url="https://discourse.example.com"
        )
        result = _fetch_discourse(config, "Arrow UDF", MagicMock())
        assert result is not None
        assert result["type"] == "discourse"
        assert len(result["posts"]) == 1

    @patch("franktheunicorn.data_access.discourse.fetcher.DiscourseFetcher")
    def test_returns_none_when_no_posts(self, mock_fetcher_cls: MagicMock) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_discourse

        mock_result = MagicMock()
        mock_result.posts = []
        mock_fetcher_cls.return_value.fetch.return_value = mock_result

        config = CommunitySourceConfig(
            type="discourse", name="Forum", base_url="https://discourse.example.com"
        )
        result = _fetch_discourse(config, "query", MagicMock())
        assert result is None


@pytest.mark.django_db
class TestFetchDiscord:
    @patch("franktheunicorn.data_access.discord.fetcher.DiscordFetcher")
    def test_returns_messages(
        self, mock_fetcher_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_discord

        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")

        msg = MagicMock()
        msg.author = "sparkdev"
        msg.content = "Check the new API"

        mock_result = MagicMock()
        mock_result.messages = [msg]
        mock_fetcher_cls.return_value.fetch.return_value = mock_result

        config = CommunitySourceConfig(
            type="discord",
            name="Spark Discord",
            bot_token_env="DISCORD_BOT_TOKEN",
            guild_id="123456",
        )
        result = _fetch_discord(config, "Arrow API", MagicMock())
        assert result is not None
        assert result["type"] == "discord"
        assert result["messages"][0]["author"] == "sparkdev"

    def test_returns_none_when_no_bot_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_discord

        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

        config = CommunitySourceConfig(
            type="discord",
            name="Discord",
            bot_token_env="DISCORD_BOT_TOKEN",
            guild_id="123",
        )
        result = _fetch_discord(config, "query", MagicMock())
        assert result is None

    @patch("franktheunicorn.data_access.discord.fetcher.DiscordFetcher")
    def test_returns_none_when_no_messages(
        self, mock_fetcher_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_discord

        monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")

        mock_result = MagicMock()
        mock_result.messages = []
        mock_fetcher_cls.return_value.fetch.return_value = mock_result

        config = CommunitySourceConfig(
            type="discord", name="Discord", bot_token_env="DISCORD_BOT_TOKEN", guild_id="123"
        )
        result = _fetch_discord(config, "query", MagicMock())
        assert result is None


@pytest.mark.django_db
class TestFetchGithubIssues:
    @patch("franktheunicorn.data_access.github.issue_fetcher.IssueFetcher")
    def test_returns_issues(self, mock_fetcher_cls: MagicMock) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_github_issues

        issue = MagicMock()
        issue.number = 42
        issue.title = "Arrow bug"
        issue.state = "open"
        issue.body = "There is a bug in Arrow"

        mock_fetcher_cls.return_value.fetch_related_issues.return_value = [issue]

        config = CommunitySourceConfig(
            type="github-issues",
            name="apache/spark issues",
            base_url="https://github.com/apache/spark",
        )
        result = _fetch_github_issues(config, "Arrow bug fix", MagicMock())
        assert result is not None
        assert result["type"] == "github-issues"
        assert result["issues"][0]["number"] == 42

    @patch("franktheunicorn.data_access.github.issue_fetcher.IssueFetcher")
    def test_returns_none_when_no_issues(self, mock_fetcher_cls: MagicMock) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_github_issues

        mock_fetcher_cls.return_value.fetch_related_issues.return_value = []

        config = CommunitySourceConfig(
            type="github-issues",
            name="issues",
            base_url="https://github.com/apache/spark",
        )
        result = _fetch_github_issues(config, "query", MagicMock())
        assert result is None

    def test_returns_none_for_bad_url(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_github_issues

        config = CommunitySourceConfig(
            type="github-issues",
            name="issues",
            base_url="noslash",
        )
        result = _fetch_github_issues(config, "query", MagicMock())
        # base_url "noslash" has no "/" so split gives < 2 parts => None
        assert result is None

    def test_uses_name_fallback(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_github_issues

        with patch("franktheunicorn.data_access.github.issue_fetcher.IssueFetcher") as mock_cls:
            issue = MagicMock()
            issue.number = 1
            issue.title = "Test"
            issue.state = "open"
            issue.body = "body"
            mock_cls.return_value.fetch_related_issues.return_value = [issue]

            config = CommunitySourceConfig(
                type="github-issues",
                name="",
                base_url="https://github.com/apache/spark",
            )
            result = _fetch_github_issues(config, "query", MagicMock())
            assert result is not None
            assert result["name"] == "apache/spark issues"


@pytest.mark.django_db
class TestFetchPerplexity:
    @patch("franktheunicorn.data_access.perplexity.fetcher.PerplexityFetcher")
    def test_returns_content(
        self, mock_fetcher_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_perplexity

        monkeypatch.setenv("PERPLEXITY_API_KEY", "fake-key")

        mock_result = MagicMock()
        mock_result.content = "Arrow is a columnar format..."
        mock_result.citations = ["https://docs.example.com"]
        mock_fetcher_cls.return_value.fetch.return_value = mock_result

        config = CommunitySourceConfig(type="perplexity", name="Perplexity")
        op_config = OperatorConfig(perplexity=PerplexityConfig(enabled=True))
        result = _fetch_perplexity(config, "Arrow API", MagicMock(), op_config)
        assert result is not None
        assert result["type"] == "perplexity"
        assert "Arrow" in result["content"]

    def test_returns_none_when_no_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_perplexity

        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)

        config = CommunitySourceConfig(type="perplexity", name="Perplexity")
        op_config = OperatorConfig(perplexity=PerplexityConfig(enabled=True))
        result = _fetch_perplexity(config, "query", MagicMock(), op_config)
        assert result is None

    def test_returns_none_when_perplexity_disabled(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_perplexity

        config = CommunitySourceConfig(type="perplexity", name="Perplexity")
        op_config = OperatorConfig(perplexity=PerplexityConfig(enabled=False))
        result = _fetch_perplexity(config, "query", MagicMock(), op_config)
        assert result is None

    @patch("franktheunicorn.data_access.perplexity.fetcher.PerplexityFetcher")
    def test_returns_none_when_empty_content(
        self, mock_fetcher_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_perplexity

        monkeypatch.setenv("PERPLEXITY_API_KEY", "fake-key")

        mock_result = MagicMock()
        mock_result.content = ""
        mock_fetcher_cls.return_value.fetch.return_value = mock_result

        config = CommunitySourceConfig(type="perplexity", name="Perplexity")
        op_config = OperatorConfig(perplexity=PerplexityConfig(enabled=True))
        result = _fetch_perplexity(config, "query", MagicMock(), op_config)
        assert result is None

    def test_returns_none_when_no_operator_config(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_perplexity

        config = CommunitySourceConfig(type="perplexity", name="Perplexity")
        result = _fetch_perplexity(config, "query", MagicMock(), None)
        assert result is None


@pytest.mark.django_db
class TestFetchSingleSource:
    def test_dispatch_mailing_list(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_single_source

        config = CommunitySourceConfig(
            type="mailing-list", name="dev@", archive_url="https://lists.example.com"
        )
        with patch(
            "franktheunicorn.data_access.context_orchestrator._fetch_mailing_list",
            return_value={"type": "mailing-list"},
        ) as mock_fn:
            result = _fetch_single_source(config, "query", MagicMock())
            mock_fn.assert_called_once()
            assert result == {"type": "mailing-list"}

    def test_dispatch_discourse(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_single_source

        config = CommunitySourceConfig(
            type="discourse", name="Forum", base_url="https://discourse.example.com"
        )
        with patch(
            "franktheunicorn.data_access.context_orchestrator._fetch_discourse",
            return_value={"type": "discourse"},
        ) as mock_fn:
            result = _fetch_single_source(config, "query", MagicMock())
            mock_fn.assert_called_once()
            assert result == {"type": "discourse"}

    def test_dispatch_discord(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_single_source

        config = CommunitySourceConfig(
            type="discord", name="Discord", bot_token_env="TOKEN", guild_id="123"
        )
        with patch(
            "franktheunicorn.data_access.context_orchestrator._fetch_discord",
            return_value={"type": "discord"},
        ) as mock_fn:
            result = _fetch_single_source(config, "query", MagicMock())
            mock_fn.assert_called_once()
            assert result == {"type": "discord"}

    def test_dispatch_github_issues(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_single_source

        config = CommunitySourceConfig(
            type="github-issues", name="issues", base_url="https://github.com/a/b"
        )
        with patch(
            "franktheunicorn.data_access.context_orchestrator._fetch_github_issues",
            return_value={"type": "github-issues"},
        ) as mock_fn:
            result = _fetch_single_source(config, "query", MagicMock())
            mock_fn.assert_called_once()
            assert result == {"type": "github-issues"}

    def test_dispatch_perplexity(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_single_source

        config = CommunitySourceConfig(type="perplexity", name="Perplexity")
        with patch(
            "franktheunicorn.data_access.context_orchestrator._fetch_perplexity",
            return_value={"type": "perplexity"},
        ) as mock_fn:
            result = _fetch_single_source(config, "query", MagicMock(), OperatorConfig())
            mock_fn.assert_called_once()
            assert result == {"type": "perplexity"}

    def test_dispatch_sentry_returns_none(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_single_source

        config = CommunitySourceConfig(type="sentry", name="Sentry")
        result = _fetch_single_source(config, "query", MagicMock())
        assert result is None

    def test_dispatch_unknown_type_returns_none(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_single_source

        # Validator warns but allows unknown types
        config = CommunitySourceConfig(type="foobar", name="mystery")
        result = _fetch_single_source(config, "query", MagicMock())
        assert result is None

    def test_not_community_source_config_returns_none(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_single_source

        result = _fetch_single_source("not a config", "query", MagicMock())
        assert result is None


@pytest.mark.django_db
class TestFetchSentryContext:
    def test_skips_when_disabled(self) -> None:
        pr = PullRequestFactory()
        op_config = OperatorConfig(sentry=SentryConfig(enabled=False))
        result = fetch_sentry_context(pr, op_config)
        assert result == ""

    def test_skips_when_no_operator_config(self) -> None:
        pr = PullRequestFactory()
        result = fetch_sentry_context(pr, None)
        assert result == ""

    def test_uses_cache_when_available(self) -> None:
        pr = PullRequestFactory(
            sentry_context_cache={
                "issues": [{"title": "NullPointerException", "count": 42, "user_count": 10}]
            }
        )
        op_config = OperatorConfig(
            sentry=SentryConfig(enabled=True, org_slug="myorg", project_slug="myproj")
        )
        result = fetch_sentry_context(pr, op_config)
        assert "NullPointerException" in result
        assert "42" in result

    def test_returns_empty_when_no_auth_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
        pr = PullRequestFactory(changed_files=["src/module.py"])
        op_config = OperatorConfig(
            sentry=SentryConfig(enabled=True, org_slug="org", project_slug="proj")
        )
        result = fetch_sentry_context(pr, op_config)
        assert result == ""

    def test_returns_empty_when_no_changed_files(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SENTRY_AUTH_TOKEN", "fake-token")
        pr = PullRequestFactory(changed_files=[])
        op_config = OperatorConfig(
            sentry=SentryConfig(enabled=True, org_slug="org", project_slug="proj")
        )
        result = fetch_sentry_context(pr, op_config)
        assert result == ""

    @patch("franktheunicorn.data_access.sentry.fetcher.SentryFetcher")
    def test_fetches_and_caches(
        self, mock_fetcher_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SENTRY_AUTH_TOKEN", "fake-token")

        mock_result = MagicMock()
        mock_result.issues = [MagicMock(title="Error", count=5, user_count=1)]
        mock_result.to_cache_dict.return_value = {
            "issues": [{"title": "Error", "count": 5, "user_count": 1}]
        }
        mock_result.to_prompt_context.return_value = "Sentry errors: Error (5 events)"
        mock_fetcher_cls.return_value.fetch_issues_for_files.return_value = mock_result

        pr = PullRequestFactory(changed_files=["src/module.py"])
        op_config = OperatorConfig(
            sentry=SentryConfig(enabled=True, org_slug="myorg", project_slug="myproj")
        )
        result = fetch_sentry_context(pr, op_config)
        assert "Sentry errors" in result
        pr.refresh_from_db()
        assert pr.sentry_context_cache is not None

    @patch("franktheunicorn.data_access.sentry.fetcher.SentryFetcher")
    def test_returns_empty_when_no_issues(
        self, mock_fetcher_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SENTRY_AUTH_TOKEN", "fake-token")

        mock_result = MagicMock()
        mock_result.issues = []
        mock_fetcher_cls.return_value.fetch_issues_for_files.return_value = mock_result

        pr = PullRequestFactory(changed_files=["src/module.py"])
        op_config = OperatorConfig(
            sentry=SentryConfig(enabled=True, org_slug="org", project_slug="proj")
        )
        result = fetch_sentry_context(pr, op_config)
        assert result == ""

    @patch("franktheunicorn.data_access.sentry.fetcher.SentryFetcher")
    def test_handles_exception_gracefully(
        self, mock_fetcher_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SENTRY_AUTH_TOKEN", "fake-token")

        mock_fetcher_cls.return_value.fetch_issues_for_files.side_effect = RuntimeError("boom")

        pr = PullRequestFactory(changed_files=["src/module.py"])
        op_config = OperatorConfig(
            sentry=SentryConfig(enabled=True, org_slug="org", project_slug="proj")
        )
        result = fetch_sentry_context(pr, op_config)
        assert result == ""


@pytest.mark.django_db
class TestFetchCommunityContext:
    def test_returns_empty_when_no_sources(self) -> None:
        pr = PullRequestFactory(title="Fix bug")
        config = ProjectConfig(owner="apache", repo="spark", community_sources=[])
        result = fetch_community_context(pr, config)
        assert result == ""

    def test_uses_cache_when_available(self) -> None:
        pr = PullRequestFactory(
            title="Fix bug",
            community_context_cache={
                "sources": [
                    {
                        "type": "mailing-list",
                        "name": "dev@spark",
                        "threads": [{"subject": "Thread", "date": "2026-01-01", "snippet": "snip"}],
                    }
                ],
                "query": "Fix bug",
            },
        )
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            community_sources=[
                CommunitySourceConfig(
                    type="mailing-list", name="dev@", archive_url="https://lists.example.com"
                )
            ],
        )
        result = fetch_community_context(pr, config)
        assert "dev@spark" in result

    def test_returns_empty_when_query_empty(self) -> None:
        pr = PullRequestFactory(title="", changed_files=[])
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            community_sources=[
                CommunitySourceConfig(
                    type="mailing-list", name="dev@", archive_url="https://lists.example.com"
                )
            ],
        )
        result = fetch_community_context(pr, config)
        assert result == ""

    @patch("franktheunicorn.data_access.context_orchestrator._fetch_single_source")
    def test_fetches_and_caches_results(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = {
            "type": "mailing-list",
            "name": "dev@spark",
            "threads": [{"subject": "Thread", "date": "2026-01-01", "snippet": "snip"}],
        }

        pr = PullRequestFactory(title="Arrow API change")
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            community_sources=[
                CommunitySourceConfig(
                    type="mailing-list", name="dev@", archive_url="https://lists.example.com"
                )
            ],
        )
        result = fetch_community_context(pr, config)
        assert "dev@spark" in result
        pr.refresh_from_db()
        assert pr.community_context_cache is not None
        assert pr.community_context_cache["query"] == "Arrow API change"

    @patch("franktheunicorn.data_access.context_orchestrator._fetch_single_source")
    def test_handles_source_exception(self, mock_fetch: MagicMock) -> None:
        mock_fetch.side_effect = RuntimeError("source failed")

        pr = PullRequestFactory(title="Arrow change")
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            community_sources=[
                CommunitySourceConfig(
                    type="mailing-list", name="dev@", archive_url="https://lists.example.com"
                )
            ],
        )
        # Should not raise
        result = fetch_community_context(pr, config)
        assert result == ""

    @patch("franktheunicorn.data_access.context_orchestrator._fetch_single_source")
    def test_skips_none_results(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = None

        pr = PullRequestFactory(title="Some PR title")
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            community_sources=[
                CommunitySourceConfig(
                    type="mailing-list", name="dev@", archive_url="https://lists.example.com"
                )
            ],
        )
        result = fetch_community_context(pr, config)
        assert result == ""

    @patch("franktheunicorn.data_access.context_orchestrator._fetch_single_source")
    def test_multiple_sources(self, mock_fetch: MagicMock) -> None:
        def side_effect(
            source_config: object, query: str, client: object, op_config: object = None
        ) -> dict[str, object] | None:
            if hasattr(source_config, "type") and source_config.type == "mailing-list":
                return {
                    "type": "mailing-list",
                    "name": "dev@spark",
                    "threads": [{"subject": "ML Thread", "date": "2026-01-01", "snippet": "s"}],
                }
            return {
                "type": "discourse",
                "name": "Forum",
                "posts": [{"title": "Forum post", "url": "https://forum.example.com/t/1"}],
            }

        mock_fetch.side_effect = side_effect

        pr = PullRequestFactory(title="Arrow change")
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            community_sources=[
                CommunitySourceConfig(
                    type="mailing-list", name="dev@", archive_url="https://lists.example.com"
                ),
                CommunitySourceConfig(
                    type="discourse", name="Forum", base_url="https://forum.example.com"
                ),
            ],
        )
        result = fetch_community_context(pr, config)
        assert "dev@spark" in result
        assert "Forum" in result


class TestWrongConfigTypeGuards:
    """Cover isinstance(config, CommunitySourceConfig) guards that return None."""

    def test_fetch_discourse_wrong_config_type(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_discourse

        result = _fetch_discourse("not a config", "query", MagicMock())
        assert result is None

    def test_fetch_discord_wrong_config_type(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_discord

        result = _fetch_discord("not a config", "query", MagicMock())
        assert result is None

    def test_fetch_github_issues_wrong_config_type(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_github_issues

        result = _fetch_github_issues("not a config", "query", MagicMock())
        assert result is None

    def test_fetch_perplexity_wrong_config_type(self) -> None:
        from franktheunicorn.data_access.context_orchestrator import _fetch_perplexity

        result = _fetch_perplexity("not a config", "query", MagicMock(), None)
        assert result is None
