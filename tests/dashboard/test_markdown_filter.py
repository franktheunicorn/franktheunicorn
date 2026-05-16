"""Tests for the render_markdown template filter."""

from __future__ import annotations

import pytest
from django.utils.safestring import SafeString

from franktheunicorn.dashboard.templatetags.markdown_filters import render_markdown


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
