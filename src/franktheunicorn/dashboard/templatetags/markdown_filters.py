from __future__ import annotations

import re

from django import template
from django.utils.safestring import SafeString, mark_safe
from markdown_it import MarkdownIt

register = template.Library()

# HTML comments (``<!-- ... -->``) are author-facing instructions that GitHub
# does not render. With ``html: False`` markdown-it does not recognise them as
# comments at all — it escapes the ``<`` and the whole marker shows up in the
# dashboard as visible ``&lt;!-- ... --&gt;`` text. PR templates are full of
# these instructions, so every templated PR body rendered as noise.
#
# They come out of the source instead of being dropped at render time, which is
# what ``html: True`` plus a comment-eating render rule did. That also turned on
# markdown-it's HTML *block* rule, and an HTML block swallows every line up to a
# blank one: a ``<div align="center">`` wrapper around a table — common in PR
# descriptions, and rendered by GitHub — came back as one escaped literal with
# the table markup unprocessed. Stripping the comments first leaves the parser
# in the configuration it was in, so raw HTML is still escaped (``<script>``,
# ``<img onerror=...>``: PR bodies are attacker-controlled) and the markdown
# around it still renders.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

#: Candidate fence line: up to three leading spaces, three or more backticks
#: or tildes, then the info string. Whether it actually opens or closes a block
#: is decided by ``_opens_fence`` / ``_closes_fence`` — matching the marker
#: alone gets both directions wrong. A line holding an inline code span opened
#: a fence that never closed, so every comment after it survived; and a code
#: line that merely starts with three backticks closed it early, deleting
#: comments out of the middle of a code block.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _opens_fence(line: str) -> str | None:
    """The marker this line opens a code fence with, or ``None``.

    CommonMark forbids a backtick anywhere in a backtick fence's info string,
    which is what separates an opening fence from an inline code span.
    """
    match = _FENCE_RE.match(line)
    if not match:
        return None
    marker, info = match.group(1), match.group(2)
    if marker[0] == "`" and "`" in info:
        return None
    return marker


def _closes_fence(line: str, marker: str) -> bool:
    """Whether this line closes a fence opened with *marker*.

    A closing fence is the same character, at least as long, and nothing but
    whitespace after it; a marker followed by prose is code, not a close.
    """
    match = _FENCE_RE.match(line)
    if not match:
        return False
    found, rest = match.group(1), match.group(2)
    return found[0] == marker[0] and len(found) >= len(marker) and not rest.strip()


def strip_html_comments(text: str) -> str:
    """Remove ``<!-- ... -->`` outside fenced code blocks.

    A comment inside a fence is content — a PR body explaining the template's
    own markers, say — and GitHub shows it, so the fence is copied through
    untouched. (A four-space-indented code block is not tracked; a comment
    there is stripped. PR bodies use fences.)
    """
    if "<!--" not in text:
        return text
    out: list[str] = []
    plain: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        if fence is None:
            marker = _opens_fence(line)
            if marker is not None:
                out.append(_HTML_COMMENT_RE.sub("", "".join(plain)))
                plain = []
                fence = marker
                out.append(line)
            else:
                plain.append(line)
        else:
            out.append(line)
            if _closes_fence(line, fence):
                fence = None
    out.append(_HTML_COMMENT_RE.sub("", "".join(plain)))
    return "".join(out)


_md = MarkdownIt("gfm-like", {"html": False})

# Title renderer: formatting only (code spans, emphasis), NO links. PR titles
# are attacker-controlled and get rendered inside the dashboard's own <a>
# elements — markdown links or linkified bare URLs would nest anchors and let
# a PR author control where clicking the title navigates.
_md_title = MarkdownIt("zero", {"html": False, "linkify": False}).enable(
    ["backticks", "emphasis", "strikethrough", "escape", "entity"]
)


@register.filter(name="render_markdown", is_safe=True)
def render_markdown(value: str | None) -> SafeString:
    if not value:
        return mark_safe("")
    rendered: str = _md.render(strip_html_comments(str(value)))
    return mark_safe(rendered)


@register.filter(name="render_markdown_inline", is_safe=True)
def render_markdown_inline(value: str | None) -> SafeString:
    """Render markdown for inline contexts (titles, link text).

    Uses the link-free title renderer: the call sites put the result inside
    anchors/headings, where author-controlled links must not appear.
    """
    if not value:
        return mark_safe("")
    rendered: str = _md_title.renderInline(strip_html_comments(str(value)))
    return mark_safe(rendered)
