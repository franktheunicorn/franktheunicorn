"""Dual-path JIRA ticket fetcher.

API path: JIRA REST API v2 ``GET /rest/api/2/issue/{key}``
Scrape path: Parse JIRA issue page HTML

Both paths return a ``JiraTicketResult`` with the same fields.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from franktheunicorn.data_access.base import (
    DataFetcher,
    FetchMethod,
)
from franktheunicorn.data_access.jira.types import JiraComment, JiraTicketResult

logger = logging.getLogger(__name__)

# Pattern to extract JIRA ticket IDs from PR title/body.
# Matches: SPARK-12345, PROJECT-123, ABC-1
TICKET_ID_PATTERN = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")


def extract_ticket_ids(text: str, project_prefix: str = "") -> list[str]:
    """Extract JIRA ticket IDs from text.

    If ``project_prefix`` is set, only return tickets matching that prefix.
    """
    matches = TICKET_ID_PATTERN.findall(text)
    if project_prefix:
        # Match the full project key — a bare startswith("SPARK") would also
        # accept SPARKR-12 and any other project sharing the prefix.
        prefix = project_prefix.upper() + "-"
        matches = [m for m in matches if m.startswith(prefix)]
    # Deduplicate while preserving order.
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


class JiraFetcher(DataFetcher[JiraTicketResult]):
    """Fetches JIRA tickets via REST API or HTML scrape."""

    def fetch_via_api(  # type: ignore[override]
        self,
        server: str,
        ticket_id: str,
    ) -> JiraTicketResult:
        """Fetch a JIRA ticket via REST API v2."""
        url = f"{server}/rest/api/2/issue/{ticket_id}"
        response = self._api_get(
            url,
            headers={"Accept": "application/json"},
        )
        data: dict[str, Any] = response.json()
        return self._parse_api_response(data, ticket_id)

    def fetch_via_scrape(  # type: ignore[override]
        self,
        server: str,
        ticket_id: str,
    ) -> JiraTicketResult:
        """Fetch a JIRA ticket by scraping the HTML page."""
        url = f"{server}/browse/{ticket_id}"
        response = self._scrape_get(url)
        return self._parse_html(response.text, ticket_id)

    @staticmethod
    def _parse_api_response(data: dict[str, Any], ticket_id: str) -> JiraTicketResult:
        """Parse JIRA REST API JSON into a JiraTicketResult."""
        fields = data.get("fields", {})

        assignee_obj = fields.get("assignee") or {}
        priority_obj = fields.get("priority") or {}
        status_obj = fields.get("status") or {}
        issuetype_obj = fields.get("issuetype") or {}
        project_obj = fields.get("project") or {}

        # Parse comments from the nested comment field.
        comments_data = fields.get("comment", {})
        raw_comments: list[dict[str, Any]] = []
        if isinstance(comments_data, dict):
            raw_comments = comments_data.get("comments", [])
        elif isinstance(comments_data, list):
            raw_comments = comments_data

        recent_comments = [
            JiraComment(
                author=_get_nested(c, "author", "displayName"),
                body=c.get("body", ""),
                created=c.get("created", ""),
            )
            for c in raw_comments[-5:]
        ]

        return JiraTicketResult(
            fetched_via=FetchMethod.API,
            ticket_id=ticket_id,
            summary=fields.get("summary", ""),
            description=fields.get("description", "") or "",
            status=status_obj.get("name", ""),
            assignee=assignee_obj.get("displayName", ""),
            priority=priority_obj.get("name", ""),
            issue_type=issuetype_obj.get("name", ""),
            project=project_obj.get("key", ""),
            recent_comments=recent_comments,
        )

    @staticmethod
    def _parse_html(html: str, ticket_id: str) -> JiraTicketResult:
        """Parse JIRA issue HTML page into a JiraTicketResult."""
        soup = BeautifulSoup(html, "html.parser")

        summary = ""
        summary_el = soup.find("h1", id="summary-val") or soup.find("title")
        if summary_el:
            summary = summary_el.get_text(strip=True)
            # Remove "[TICKET-ID] " prefix from title.
            if summary.startswith(f"[{ticket_id}]"):
                summary = summary[len(f"[{ticket_id}]") :].strip()
            # <title>-derived summaries carry the site suffix — strip it so
            # the scrape path matches the API path's clean summary.
            for suffix in (" - ASF JIRA", " - JIRA"):
                if summary.endswith(suffix):
                    summary = summary[: -len(suffix)].strip()
                    break

        description = ""
        desc_el = soup.find("div", id="description-val") or soup.find(
            "div", class_="user-content-block"
        )
        if desc_el:
            description = desc_el.get_text(strip=True)

        status = ""
        status_el = soup.find("span", id="status-val") or soup.find(
            "span", class_="jira-issue-status-lozenge"
        )
        if status_el:
            status = status_el.get_text(strip=True)

        assignee = ""
        assignee_el = soup.find("span", id="assignee-val")
        if assignee_el:
            assignee = assignee_el.get_text(strip=True)

        priority = ""
        priority_el = soup.find("span", id="priority-val")
        if priority_el:
            priority = priority_el.get_text(strip=True)

        # Parse comments from the activity section.
        comments: list[JiraComment] = []
        comment_blocks = soup.find_all("div", class_="activity-comment")
        for block in comment_blocks[-5:]:
            author_el = block.find("a", class_="user-hover")
            body_el = block.find("div", class_="action-body")
            date_el = block.find("time")
            comments.append(
                JiraComment(
                    author=author_el.get_text(strip=True) if author_el else "",
                    body=body_el.get_text(strip=True) if body_el else "",
                    created=str(date_el.get("datetime", "")) if date_el else "",
                )
            )

        return JiraTicketResult(
            fetched_via=FetchMethod.SCRAPE,
            ticket_id=ticket_id,
            summary=summary,
            description=description,
            status=status,
            assignee=assignee,
            priority=priority,
            recent_comments=comments,
        )


def _get_nested(d: dict[str, Any], *keys: str) -> str:
    """Safely get a nested value from a dict."""
    current: Any = d
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key, "")
        else:
            return ""
    return str(current) if current else ""
