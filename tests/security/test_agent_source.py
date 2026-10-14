"""Tests for the agent-source scope: names, parsing, filters and the Source row."""

from __future__ import annotations

from typing import Any

import pytest

from franktheunicorn.core.models import SecurityReport
from franktheunicorn.security import agent_source
from tests.factories import SecurityReportFactory


class TestNames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Claude Code", "claude-code"),
            ("  XBOW ", "xbow"),
            ("gpt-5.1/codex", "gpt-5.1-codex"),
            ("big_sleep", "big_sleep"),
            ("--weird--name..", "weird-name"),
            ("&&&", ""),
        ],
    )
    def test_normalize_folds_to_one_spelling(self, raw: str, expected: str) -> None:
        assert agent_source.normalize_agent_name(raw) == expected

    def test_normalize_caps_at_the_column_width(self) -> None:
        name = agent_source.normalize_agent_name("a" * 300)
        assert len(name) == agent_source.MAX_AGENT_NAME_LEN

    def test_blank_is_a_person(self) -> None:
        assert agent_source.clean_agent_name("   ") == ""

    def test_clean_returns_the_stored_spelling(self) -> None:
        assert agent_source.clean_agent_name("Claude Code") == "claude-code"

    @pytest.mark.parametrize("reserved", ["all", "None", " ANY "])
    def test_reserved_scope_words_are_refused(self, reserved: str) -> None:
        """A scope must never mean two things, so the keywords can't be names."""
        with pytest.raises(ValueError, match="reserved"):
            agent_source.clean_agent_name(reserved)

    def test_input_that_folds_to_nothing_is_refused_not_stored_blank(self) -> None:
        """Storing "" would file an agent's whole archive as human-sent."""
        with pytest.raises(ValueError, match="isn't usable"):
            agent_source.clean_agent_name("!!!")


class TestParseScope:
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_absent_is_all(self, raw: str | None) -> None:
        assert agent_source.parse_scope(raw) == agent_source.SCOPE_ALL

    @pytest.mark.parametrize("raw", ["all", "none", "any"])
    def test_keywords_pass_through(self, raw: str) -> None:
        assert agent_source.parse_scope(raw) == raw

    def test_a_name_is_normalised(self) -> None:
        assert agent_source.parse_scope("Claude Code") == "claude-code"

    def test_junk_is_none_not_all(self) -> None:
        """Callers about to spend money refuse on None; widening to all is the bug."""
        assert agent_source.parse_scope("&&&") is None


@pytest.mark.django_db
class TestScopeQ:
    def _backlog(self) -> dict[str, Any]:
        return {
            "human": SecurityReportFactory(source_agent=""),
            "xbow": SecurityReportFactory(source_agent="xbow"),
            "claude": SecurityReportFactory(source_agent="claude-code"),
        }

    def _selected(self, scope: str) -> set[int]:
        return set(
            SecurityReport.objects.filter(agent_source.scope_q(scope)).values_list("pk", flat=True)
        )

    def test_each_scope_selects_its_reports(self) -> None:
        rows = self._backlog()
        pk = {key: report.pk for key, report in rows.items()}

        assert self._selected("all") == set(pk.values())
        assert self._selected("none") == {pk["human"]}
        assert self._selected("any") == {pk["xbow"], pk["claude"]}
        assert self._selected("xbow") == {pk["xbow"]}
        # A name nobody imported under selects nothing — the truth, not an error.
        assert self._selected("nobody") == set()

    def test_labels_and_suffixes(self) -> None:
        assert agent_source.scope_label("all") == "all reports"
        assert agent_source.scope_label("none") == "reports not from an agent"
        assert agent_source.scope_label("any") == "reports from any agent"
        assert agent_source.scope_label("xbow") == "reports from agent xbow"
        assert agent_source.scope_suffix("all") == ""
        assert agent_source.scope_suffix("xbow") == " (reports from agent xbow)"


@pytest.mark.django_db
class TestScopeOptions:
    def test_counts_add_up_and_carry_the_ruling_breakdown(self) -> None:
        SecurityReportFactory(source_agent="", status="valid")
        SecurityReportFactory(source_agent="xbow", status="valid")
        SecurityReportFactory(source_agent="xbow", status="invalid")
        SecurityReportFactory(source_agent="xbow", status="new")
        SecurityReportFactory(source_agent="claude-code", status="duplicate")

        options = {option.key: option for option in agent_source.scope_options()}

        assert [key for key in options] == ["all", "none", "any", "claude-code", "xbow"]
        assert options["all"].count == 5
        assert options["none"].count == 1
        assert options["any"].count == 4
        assert options["xbow"].count == 3
        # "How often is this agent right?" — the hover text answers it.
        assert options["xbow"].breakdown == (
            "1 ruled valid, 1 invalid / expected / duplicate, 1 still open"
        )
        assert options["any"].breakdown.startswith("1 ruled valid, 2 invalid")

    def test_no_any_agent_row_until_an_agent_has_reports(self) -> None:
        SecurityReportFactory(source_agent="")

        keys = [option.key for option in agent_source.scope_options()]

        assert keys == ["all", "none"]

    def test_an_empty_backlog_still_offers_the_fixed_rows(self) -> None:
        options = agent_source.scope_options()

        assert [(option.key, option.count) for option in options] == [("all", 0), ("none", 0)]

    def test_known_agents_lists_each_name_once(self) -> None:
        SecurityReportFactory(source_agent="xbow")
        SecurityReportFactory(source_agent="xbow")
        SecurityReportFactory(source_agent="claude-code")
        SecurityReportFactory(source_agent="")

        assert agent_source.known_agents() == ["claude-code", "xbow"]
