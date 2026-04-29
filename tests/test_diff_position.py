"""Tests for the unified-diff line→position translator (Gitea/Forgejo)."""

from __future__ import annotations

import pytest

from franktheunicorn.backends.diff_position import translate_line_to_position

SINGLE_HUNK = """\
diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -10,3 +10,4 @@
 unchanged
-old line
+new line one
+new line two
 trailing context
"""

MULTI_HUNK = """\
diff --git a/bar.py b/bar.py
index aaa..bbb 100644
--- a/bar.py
+++ b/bar.py
@@ -1,3 +1,3 @@
 line1
-line2
+LINE2
 line3
@@ -20,2 +20,3 @@
 line20
+line21
 line22
"""

MULTI_FILE = """\
diff --git a/a.py b/a.py
index 111..222 100644
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 keep
+added in a
 keep2
diff --git a/b.py b/b.py
index 333..444 100644
--- a/b.py
+++ b/b.py
@@ -5,2 +5,3 @@
 keep_b
+added in b
 keep_b2
"""

NEW_FILE = """\
diff --git a/new.py b/new.py
new file mode 100644
index 0000000..1234567
--- /dev/null
+++ b/new.py
@@ -0,0 +1,3 @@
+first line
+second line
+third line
"""

DELETED_FILE = """\
diff --git a/gone.py b/gone.py
deleted file mode 100644
index 1234567..0000000
--- a/gone.py
+++ /dev/null
@@ -1,3 +0,0 @@
-line one
-line two
-line three
"""


def test_added_line_in_single_hunk() -> None:
    # @@ at line 10. Lines: " unchanged"=pos1, "-old"=pos2, "+new line one"=pos3.
    pos = translate_line_to_position(SINGLE_HUNK, "foo.py", 11, side="RIGHT")
    assert pos == 3


def test_context_line_right_side() -> None:
    # " unchanged" is new-side line 10 → position 1.
    pos = translate_line_to_position(SINGLE_HUNK, "foo.py", 10, side="RIGHT")
    assert pos == 1


def test_context_line_left_side() -> None:
    # " unchanged" is old-side line 10 → position 1.
    pos = translate_line_to_position(SINGLE_HUNK, "foo.py", 10, side="LEFT")
    assert pos == 1


def test_removed_line_left_side() -> None:
    # "-old line" is old-side line 11 → position 2.
    pos = translate_line_to_position(SINGLE_HUNK, "foo.py", 11, side="LEFT")
    assert pos == 2


def test_line_outside_diff_returns_none() -> None:
    assert translate_line_to_position(SINGLE_HUNK, "foo.py", 999, side="RIGHT") is None


def test_file_not_in_diff_returns_none() -> None:
    assert translate_line_to_position(SINGLE_HUNK, "other.py", 10, side="RIGHT") is None


def test_multi_hunk_subsequent_hunk_position_includes_header() -> None:
    # First hunk: " line1"=1, "-line2"=2, "+LINE2"=3, " line3"=4.
    # Second hunk header: pos 5. " line20"=6, "+line21"=7, " line22"=8.
    # New-side line 21 = "+line21" → position 7.
    pos = translate_line_to_position(MULTI_HUNK, "bar.py", 21, side="RIGHT")
    assert pos == 7


def test_multi_hunk_first_hunk_change() -> None:
    # "+LINE2" is new-side line 2 in the first hunk → position 3.
    pos = translate_line_to_position(MULTI_HUNK, "bar.py", 2, side="RIGHT")
    assert pos == 3


def test_multi_file_targets_correct_file() -> None:
    # In b.py: " keep_b"=1, "+added in b"=2.
    pos = translate_line_to_position(MULTI_FILE, "b.py", 6, side="RIGHT")
    assert pos == 2


def test_multi_file_does_not_leak_position_across_files() -> None:
    # In a.py: " keep"=1, "+added in a"=2, " keep2"=3.
    pos = translate_line_to_position(MULTI_FILE, "a.py", 2, side="RIGHT")
    assert pos == 2


def test_new_file_added_line() -> None:
    # New file: "+first"=1, "+second"=2, "+third"=3.
    pos = translate_line_to_position(NEW_FILE, "new.py", 2, side="RIGHT")
    assert pos == 2


def test_new_file_left_side_returns_none() -> None:
    assert translate_line_to_position(NEW_FILE, "new.py", 1, side="LEFT") is None


def test_deleted_file_left_side() -> None:
    # Deleted file: "-line one"=1, "-line two"=2, "-line three"=3.
    pos = translate_line_to_position(DELETED_FILE, "gone.py", 2, side="LEFT")
    assert pos == 2


def test_deleted_file_right_side_returns_none() -> None:
    assert translate_line_to_position(DELETED_FILE, "gone.py", 1, side="RIGHT") is None


def test_invalid_side_raises() -> None:
    with pytest.raises(ValueError, match="must be 'RIGHT' or 'LEFT'"):
        translate_line_to_position(SINGLE_HUNK, "foo.py", 10, side="middle")


def test_empty_diff_returns_none() -> None:
    assert translate_line_to_position("", "foo.py", 10) is None
