"""Tests for the render_markdown and render_markdown_inline template filters."""

from __future__ import annotations

import pytest
from django.utils.safestring import SafeString

from franktheunicorn.dashboard.templatetags.markdown_filters import (
    render_markdown,
    render_markdown_inline,
)


@pytest.mark.parametrize(
    "input_md, expected_fragment",
    [
        ("**bold**", "<strong>bold</strong>"),
        ("`code`", "<code>code</code>"),
        ("```\nblock\n```", "<pre><code>"),
        ("- item", "<li>item</li>"),
        (None, ""),
        ("", ""),
        ("<script>alert(1)</script>", "&lt;script&gt;"),
        ("| a | b |\n|---|---|\n| 1 | 2 |", "<table>"),
    ],
)
def test_render_markdown(input_md: str | None, expected_fragment: str) -> None:
    result = render_markdown(input_md)
    assert expected_fragment in result


def test_render_markdown_returns_safe_string() -> None:
    result = render_markdown("hello")
    assert isinstance(result, SafeString)


def test_render_markdown_gfm_table_has_thead() -> None:
    result = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<thead>" in result


@pytest.mark.parametrize(
    "input_md, expected_fragment",
    [
        ("**bold**", "<strong>bold</strong>"),
        ("`code`", "<code>code</code>"),
        (None, ""),
        ("", ""),
        ("<script>alert(1)</script>", "&lt;script&gt;"),
    ],
)
def test_render_markdown_inline(input_md: str | None, expected_fragment: str) -> None:
    result = render_markdown_inline(input_md)
    assert expected_fragment in result


def test_render_markdown_inline_no_wrapping_paragraph() -> None:
    result = render_markdown_inline("**bold**")
    assert "<p>" not in result
    assert "</p>" not in result


def test_render_markdown_inline_returns_safe_string() -> None:
    result = render_markdown_inline("hello")
    assert isinstance(result, SafeString)


def test_render_markdown_inline_never_emits_links() -> None:
    """PR titles are attacker-controlled and rendered inside the dashboard's
    own anchors — markdown links and bare URLs must stay inert text, or the
    author controls where clicking the title navigates."""
    md_link = render_markdown_inline("[Fix everything](https://evil.example)")
    assert "<a" not in md_link
    assert "evil.example" in md_link  # kept as visible text

    bare_url = render_markdown_inline("Fix http://evil.example handling")
    assert "<a" not in bare_url

    autolink = render_markdown_inline("Fix <https://evil.example> handling")
    assert "<a" not in autolink
