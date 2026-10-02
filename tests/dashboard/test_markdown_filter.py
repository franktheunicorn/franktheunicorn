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


@pytest.mark.parametrize(
    "input_md",
    [
        # PR-template instructions, the common case.
        "<!-- Describe your changes here -->",
        # The managed marker appended to posted comments.
        "Done.\n\n---\n<sub>Generated with assistance of franktheunicorn</sub>\n"
        "<!-- franktheunicorn-managed -->",
        # Inline comment mixed with text.
        "Text <!-- foo --> more",
        # Multiline comment.
        "before\n<!-- comment\nspanning\nlines -->\nafter",
    ],
)
def test_render_markdown_hides_html_comments(input_md: str) -> None:
    """GitHub does not render HTML comments; the dashboard must not either.

    With ``html: False`` markdown-it escaped the ``<`` and the whole marker
    showed up as visible ``&lt;!-- ... --&gt;`` text, so every templated PR
    body rendered as a wall of instruction noise.
    """
    result = render_markdown(input_md)
    assert "<!--" not in result
    assert "-->" not in result
    assert "&lt;!--" not in result


def test_render_markdown_preserves_html_comments_inside_code() -> None:
    """A comment inside a code block is code, not a comment — keep it."""
    result = render_markdown("```\n<!-- real code -->\n```")
    assert "&lt;!-- real code --&gt;" in result


def test_render_markdown_escapes_raw_html_other_than_comments() -> None:
    """Raw HTML that is not a comment stays escaped — bodies are attacker-controlled."""
    assert render_markdown("<b>bold</b>") == "<p>&lt;b&gt;bold&lt;/b&gt;</p>\n"
    assert "<img" not in render_markdown("<img src=x onerror=alert(1)>")
    assert "&lt;img" in render_markdown("<img src=x onerror=alert(1)>")


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


def test_markdown_around_an_html_wrapper_still_renders() -> None:
    """A ``<div>`` wrapper must not swallow the markdown inside it.

    ``html: True`` turned on markdown-it's HTML *block* rule, which consumes
    every line to the next blank one. A table wrapped in ``<div align="center">``
    — common in PR descriptions, and rendered by GitHub — came back as one
    escaped literal with the pipes unprocessed.
    """
    body = '<div align="center">\n| a | b |\n|---|---|\n| 1 | 2 |\n</div>'

    result = render_markdown(body)

    assert "<table>" in result
    assert "| a | b |" not in result
    # The tag itself is still inert text, not emitted HTML.
    assert "&lt;div" in result
    assert "<div" not in result


def test_a_comment_between_fenced_blocks_is_still_stripped() -> None:
    """Fence tracking must close, or everything after the first block survives."""
    body = "```\ncode\n```\n<!-- hide -->\n```\nmore\n```\n"

    result = render_markdown(body)

    assert "&lt;!--" not in result
    assert "<!--" not in result
    assert result.count("<pre>") == 2


def test_a_tilde_fence_protects_its_comment() -> None:
    result = render_markdown("~~~\n<!-- keep -->\n~~~\n")

    assert "&lt;!-- keep --&gt;" in result


def test_an_unclosed_fence_keeps_what_follows_verbatim() -> None:
    """Degrades the way the parser does: everything after it is code."""
    result = render_markdown("```\n<!-- inside -->\n")

    assert "&lt;!-- inside --&gt;" in result
