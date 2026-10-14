"""Live @-mentions in text frank posts are backtick-quoted; code is left alone."""

from __future__ import annotations

from franktheunicorn.backends.mentions import escape_handles, mention_scan, normalize_allowlist


class TestEscapeHandles:
    def test_a_prose_handle_is_quoted(self) -> None:
        assert escape_handles("ask @alice about this") == "ask `@alice` about this"

    def test_a_team_handle_is_quoted_whole(self) -> None:
        assert escape_handles("cc @apache/spark-committers") == "cc `@apache/spark-committers`"

    def test_code_spans_and_fences_are_untouched(self) -> None:
        text = "see `@alice` and\n```python\n@alice\n```\nthen @bob"
        assert escape_handles(text) == "see `@alice` and\n```python\n@alice\n```\nthen `@bob`"

    def test_decorators_with_a_dot_or_call_are_untouched(self) -> None:
        text = "@pytest.mark.django_db\n@app.route('/x')\n@property"
        out = escape_handles(text)
        assert out.splitlines()[0] == "@pytest.mark.django_db"
        assert out.splitlines()[1] == "@app.route('/x')"
        # A bare @word alone on a line is a mention, not a decorator (Magpie
        # treats it as a decorator and lets it through live).
        assert out.splitlines()[2] == "`@property`"

    def test_emails_and_already_quoted_handles_are_not_mentions(self) -> None:
        text = "mail alice@example.com or see `@alice`"
        assert escape_handles(text) == text

    def test_cron_aliases_and_the_allowlist_stay_live(self) -> None:
        allowed = normalize_allowlist(["@Alice", " bob "])
        text = "runs @daily; thanks @alice and @BOB, not @carol"
        assert escape_handles(text, allowed) == "runs @daily; thanks @alice and @BOB, not `@carol`"


class TestMentionScan:
    def test_reports_each_live_handle_with_its_line(self) -> None:
        hits = mention_scan("first @alice\n```\n@ignored\n```\nthen @bob and `@quoted`")
        assert [(h.handle, h.line) for h in hits] == [("alice", 1), ("bob", 5)]

    def test_allowlisted_and_cron_are_skipped(self) -> None:
        assert mention_scan("@daily @alice", normalize_allowlist(["alice"])) == []
