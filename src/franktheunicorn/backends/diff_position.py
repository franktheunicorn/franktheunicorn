"""
Translate file/line/side coordinates into a positional offset within a
unified diff, as required by the Gitea/Forgejo review API and (for the
historical-position form) GitHub.

The "position" is 1-based, counted from the line immediately after the
first ``@@`` hunk header for the target file. It increments for every
subsequent line within that file's hunks (including subsequent ``@@``
headers, blank context lines, and ``+``/``-`` lines) until the start of
the next file. This matches the original GitHub Reviews API "position"
semantics, which Gitea adopted.
"""

from __future__ import annotations

import re

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE_HEADER_NEW = re.compile(r"^\+\+\+ b/(.+)$")
_FILE_HEADER_NEW_NULL = re.compile(r"^\+\+\+ /dev/null$")
_FILE_HEADER_OLD = re.compile(r"^--- a/(.+)$")


def translate_line_to_position(
    diff_text: str,
    file_path: str,
    line: int,
    side: str = "RIGHT",
) -> int | None:
    """Return the diff position for ``(file_path, line, side)``, or ``None``.

    ``side`` is ``"RIGHT"`` (the new/added side, matching ``+`` lines) or
    ``"LEFT"`` (the old/removed side, matching ``-`` lines). Context lines
    (`` ``) match either side.

    Returns ``None`` if the file is not in the diff or the line falls
    outside any hunk for that file.
    """
    side = side.upper()
    if side not in ("RIGHT", "LEFT"):
        msg = f"side must be 'RIGHT' or 'LEFT', got {side!r}"
        raise ValueError(msg)

    in_target_file = False
    position = 0
    new_line = 0  # next physical line number on the new side
    old_line = 0  # next physical line number on the old side

    for raw in diff_text.splitlines():
        new_match = _FILE_HEADER_NEW.match(raw)
        if new_match:
            in_target_file = new_match.group(1) == file_path
            position = 0
            continue
        if _FILE_HEADER_NEW_NULL.match(raw):
            # File deleted; LEFT-side commenting is matched by the prior
            # ``--- a/<path>`` header, so leave in_target_file alone.
            position = 0
            continue
        old_match = _FILE_HEADER_OLD.match(raw)
        if old_match:
            in_target_file = side == "LEFT" and old_match.group(1) == file_path
            position = 0
            continue
        if raw.startswith("diff --git "):
            in_target_file = False
            position = 0
            continue

        if not in_target_file:
            continue

        hunk = _HUNK_HEADER.match(raw)
        if hunk:
            old_line = int(hunk.group(1))
            new_line = int(hunk.group(3))
            # Position increments for the @@ line itself only after the
            # first hunk in a file (subsequent hunk headers count too).
            if position > 0:
                position += 1
            continue

        if position == 0 and not raw.startswith(("+", "-", " ", "\\")):
            # Still walking metadata (index/mode lines); skip.
            continue

        position += 1

        if raw.startswith("\\"):
            # "\ No newline at end of file" — does not advance line numbers.
            continue
        if raw.startswith("+"):
            if side == "RIGHT" and new_line == line:
                return position
            new_line += 1
        elif raw.startswith("-"):
            if side == "LEFT" and old_line == line:
                return position
            old_line += 1
        else:
            # Context line — advances both sides.
            if side == "RIGHT" and new_line == line:
                return position
            if side == "LEFT" and old_line == line:
                return position
            new_line += 1
            old_line += 1

    return None
