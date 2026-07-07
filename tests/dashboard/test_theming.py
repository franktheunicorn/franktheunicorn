"""Guards for the consolidated dashboard theming.

The dashboard used to carry two conflicting custom-property sets: a
light-only inline <style> in base.html that overrode styles.css's dark
mode. These tests lock in the single-source-of-truth arrangement.
"""

from __future__ import annotations

from pathlib import Path

import franktheunicorn.dashboard as dashboard_pkg

_STATIC = Path(dashboard_pkg.__file__).parent / "static" / "dashboard"
_TEMPLATES = Path(dashboard_pkg.__file__).parent / "templates" / "dashboard"


def test_base_html_has_no_inline_style_block() -> None:
    base = (_TEMPLATES / "base.html").read_text()
    assert "<style>" not in base, "CSS belongs in styles.css, not an inline block"


def test_styles_css_defines_dark_mode_for_theme_variables() -> None:
    css = (_STATIC / "styles.css").read_text()
    assert "prefers-color-scheme: dark" in css
    # Dark mode must override the body/surface variables the templates use.
    dark_block = css.split("prefers-color-scheme: dark", 1)[1]
    for var in ("--bg:", "--card:", "--text:", "--border:"):
        assert var in dark_block, f"dark mode must override {var}"


def test_styles_css_has_no_dead_rules() -> None:
    css = (_STATIC / "styles.css").read_text()
    # These selectors were unused across all templates.
    assert ".agent-log" not in css
    assert ".font-mono" not in css
