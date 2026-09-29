from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from django import template
from django.utils.safestring import SafeString, mark_safe
from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.token import Token

register = template.Library()

# HTML comments (``<!-- ... -->``) are author-facing instructions that GitHub
# does not render. With ``html: False`` markdown-it does not recognise them as
# comments at all — it escapes the ``<`` and the whole marker shows up in the
# dashboard as visible ``&lt;!-- ... --&gt;`` text. PR templates are full of
# these instructions, so every templated PR body rendered as noise.
#
# Enabling raw-HTML parsing lets markdown-it see the comments, and the
# overridden ``html_block`` / ``html_inline`` render rules then drop them
# (matching GitHub) while escaping every *other* raw tag. Comment bodies and
# PR descriptions are attacker-controlled, so raw HTML must never pass through
# unescaped — ``<script>`` and ``<img onerror=...>`` stay escaped, same as
# the old ``html: False`` behaviour.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

_md = MarkdownIt("gfm-like", {"html": True})


def _render_html_filtered(
    self: Any,
    tokens: Sequence[Token],
    idx: int,
    options: Any,
    env: Any,
) -> str:
    """Render rule for raw HTML: hide comments, escape everything else.

    Registered for both ``html_block`` and ``html_inline`` tokens. A token
    whose content is a single HTML comment renders as nothing (GitHub hides
    them); any other raw HTML is escaped via markdown-it's own escaper so it
    shows as inert text rather than executing.
    """
    content = tokens[idx].content
    if _HTML_COMMENT_RE.fullmatch(content.strip()):
        return ""
    return escapeHtml(content)


_md.add_render_rule("html_block", _render_html_filtered)
_md.add_render_rule("html_inline", _render_html_filtered)

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
    rendered: str = _md.render(str(value))
    return mark_safe(rendered)


@register.filter(name="render_markdown_inline", is_safe=True)
def render_markdown_inline(value: str | None) -> SafeString:
    """Render markdown for inline contexts (titles, link text).

    Uses the link-free title renderer: the call sites put the result inside
    anchors/headings, where author-controlled links must not appear.
    """
    if not value:
        return mark_safe("")
    rendered: str = _md_title.renderInline(str(value))
    return mark_safe(rendered)
