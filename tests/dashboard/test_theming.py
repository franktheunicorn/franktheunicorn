"""Guards for the consolidated dashboard theming.

The dashboard used to carry two conflicting custom-property sets: a
light-only inline <style> in base.html that overrode styles.css's dark
mode. These tests lock in the single-source-of-truth arrangement.
"""

from __future__ import annotations

import re
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


def test_color_scheme_is_declared() -> None:
    """Without it the UA paints form controls with its LIGHT defaults on a dark
    page, and `body { color: var(--text) }` (near-white) is inherited into them —
    a <textarea> or <select> rendered as a white box with white text."""
    css = (_STATIC / "styles.css").read_text()

    assert "color-scheme: light dark" in css


#: `background: #f5f5f5` in a template survives dark mode unchanged while --text
#: goes near-white, which is how the PR body became a big white box with white
#: text in it. A hex inside ``var(--x, #hex)`` is a fallback for an undefined
#: variable, not a hardcoded colour, so those are allowed.
_BACKGROUND_HEX = re.compile(r"background(?:-color)?:\s*#[0-9a-fA-F]{3,8}")


def test_no_template_hardcodes_a_background_colour() -> None:
    offenders = []
    for path in sorted(_TEMPLATES.glob("*.html")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if _BACKGROUND_HEX.search(line):
                offenders.append(f"{path.name}:{number}")

    assert not offenders, (
        "hardcoded background colours don't follow the dark scheme; "
        f"use a --card/--*-bg variable instead: {offenders}"
    )


def test_no_template_puts_white_text_on_a_theme_accent() -> None:
    """--red and --orange are light pastels in the dark scheme (#fca5a5,
    #fdba74), so `background: var(--red); color: #fff` is unreadable there.
    Paired tint/foreground (--red-bg with --red) works in both."""
    offenders = []
    for path in sorted(_TEMPLATES.glob("*.html")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"background:\s*var\(--(?:red|orange|green|blue|yellow)\)", line) and (
                "#fff" in line
            ):
                offenders.append(f"{path.name}:{number}")

    assert not offenders, f"white on a theme accent is unreadable in dark mode: {offenders}"
