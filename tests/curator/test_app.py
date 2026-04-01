"""Tests for the Textual TUI curator app."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from franktheunicorn.curator.app import CommentView, CuratorApp, EditScreen
from franktheunicorn.curator.classifier import ClassifiedComment
from franktheunicorn.curator.scraper import RawComment


def _make_raw(body: str = "Fix this bug", **kwargs: object) -> RawComment:
    defaults = {
        "author": "alice",
        "body": body,
        "diff_context": "@@ -1 +1 @@\n-old\n+new",
        "file_path": "src/main.py",
        "pr_number": 42,
        "pr_title": "Fix bug",
        "created_at": "2026-03-20T10:00:00Z",
        "url": "https://github.com/org/repo/pull/42#r1",
    }
    defaults.update(kwargs)
    return RawComment(**defaults)


def _make_classified(
    body: str = "Fix this bug",
    category: str = "correctness",
    tone_flagged: bool = False,
    tone_flags: list[str] | None = None,
) -> ClassifiedComment:
    return ClassifiedComment(
        raw=_make_raw(body=body),
        category=category,
        tone_flagged=tone_flagged,
        tone_flags=tone_flags or [],
    )


class TestCuratorAppInstantiation:
    """Test CuratorApp can be instantiated with expected attributes."""

    def test_basic_instantiation(self) -> None:
        comments = [_make_classified(), _make_classified(body="LGTM")]
        app = CuratorApp(comments=comments, project_name="org/repo")

        assert app.comments == comments
        assert app.project_name == "org/repo"
        assert app.output_dir is None
        assert app.current_index == 0
        assert app.decisions == []

    def test_instantiation_with_output_dir(self, tmp_path: Path) -> None:
        comments = [_make_classified()]
        app = CuratorApp(
            comments=comments,
            project_name="org/repo",
            output_dir=tmp_path,
        )

        assert app.output_dir == tmp_path

    def test_empty_comments(self) -> None:
        app = CuratorApp(comments=[], project_name="org/repo")

        assert app.comments == []
        assert len(app.comments) == 0

    def test_title(self) -> None:
        app = CuratorApp(comments=[], project_name="org/repo")

        assert app.TITLE == "franktheunicorn \u2014 Comment Curator"

    def test_bindings_defined(self) -> None:
        app = CuratorApp(comments=[], project_name="org/repo")
        binding_keys = [b[0] for b in app.BINDINGS]

        assert "i" in binding_keys
        assert "e" in binding_keys
        assert "d" in binding_keys
        assert "s" in binding_keys
        assert "q" in binding_keys

    def test_css_is_nonempty(self) -> None:
        assert CuratorApp.CSS
        assert "CommentView" in CuratorApp.CSS


class TestRecordDecision:
    """Test the _record_decision method logic without running the TUI."""

    def test_record_include_decision(self) -> None:
        comments = [_make_classified(body="Fix null check")]
        app = CuratorApp(comments=comments, project_name="org/repo")

        # Mock _advance to prevent TUI calls
        app._advance = MagicMock()  # type: ignore[method-assign]
        app._record_decision("include")

        assert len(app.decisions) == 1
        decision = app.decisions[0]
        assert decision.decision == "include"
        assert decision.comment.raw.body == "Fix null check"
        assert decision.edited_body == ""
        assert decision.note == ""

    def test_record_exclude_decision(self) -> None:
        comments = [_make_classified()]
        app = CuratorApp(comments=comments, project_name="org/repo")
        app._advance = MagicMock()  # type: ignore[method-assign]

        app._record_decision("exclude")

        assert app.decisions[0].decision == "exclude"

    def test_record_skip_decision(self) -> None:
        comments = [_make_classified()]
        app = CuratorApp(comments=comments, project_name="org/repo")
        app._advance = MagicMock()  # type: ignore[method-assign]

        app._record_decision("skip")

        assert app.decisions[0].decision == "skip"

    def test_record_with_edited_body(self) -> None:
        comments = [_make_classified(body="Original")]
        app = CuratorApp(comments=comments, project_name="org/repo")
        app._advance = MagicMock()  # type: ignore[method-assign]

        app._record_decision("include", edited_body="Edited version")

        decision = app.decisions[0]
        assert decision.edited_body == "Edited version"

    def test_record_with_note(self) -> None:
        comments = [_make_classified()]
        app = CuratorApp(comments=comments, project_name="org/repo")
        app._advance = MagicMock()  # type: ignore[method-assign]

        app._record_decision("include", note="Good example")

        assert app.decisions[0].note == "Good example"

    def test_record_calls_advance(self) -> None:
        comments = [_make_classified()]
        app = CuratorApp(comments=comments, project_name="org/repo")
        app._advance = MagicMock()  # type: ignore[method-assign]

        app._record_decision("include")

        app._advance.assert_called_once()


class TestFinish:
    """Test the _finish method."""

    def test_finish_with_decisions_writes_dataset(self, tmp_path: Path) -> None:
        comments = [_make_classified(body="Fix null check")]
        app = CuratorApp(comments=comments, project_name="org/repo", output_dir=tmp_path)
        app._advance = MagicMock()  # type: ignore[method-assign]
        app._record_decision("include")

        # Mock exit and notify to avoid TUI errors
        app.exit = MagicMock()  # type: ignore[method-assign]
        app.notify = MagicMock()  # type: ignore[method-assign]

        app._finish()

        app.exit.assert_called_once()
        app.notify.assert_called_once()
        # Verify dataset was actually written
        output_file = tmp_path / "org" / "repo" / "voice_curated.jsonl"
        assert output_file.exists()

    def test_finish_with_no_decisions_exits(self) -> None:
        app = CuratorApp(comments=[], project_name="org/repo")
        app.exit = MagicMock()  # type: ignore[method-assign]

        app._finish()

        app.exit.assert_called_once()


class TestAdvance:
    """Test the _advance method."""

    def test_advance_increments_index(self) -> None:
        comments = [_make_classified(), _make_classified()]
        app = CuratorApp(comments=comments, project_name="org/repo")
        app._show_current = MagicMock()  # type: ignore[method-assign]

        assert app.current_index == 0
        app._advance()
        assert app.current_index == 1


class TestActionMethods:
    """Test the action methods guard against out-of-bounds."""

    def test_action_include_at_end_does_nothing(self) -> None:
        app = CuratorApp(comments=[], project_name="org/repo")
        app._record_decision = MagicMock()  # type: ignore[method-assign]

        app.action_include()

        app._record_decision.assert_not_called()

    def test_action_exclude_at_end_does_nothing(self) -> None:
        app = CuratorApp(comments=[], project_name="org/repo")
        app._record_decision = MagicMock()  # type: ignore[method-assign]

        app.action_exclude()

        app._record_decision.assert_not_called()

    def test_action_skip_at_end_does_nothing(self) -> None:
        app = CuratorApp(comments=[], project_name="org/repo")
        app._record_decision = MagicMock()  # type: ignore[method-assign]

        app.action_skip()

        app._record_decision.assert_not_called()

    def test_action_include_records_include(self) -> None:
        comments = [_make_classified()]
        app = CuratorApp(comments=comments, project_name="org/repo")
        app._record_decision = MagicMock()  # type: ignore[method-assign]

        app.action_include()

        app._record_decision.assert_called_once_with("include")

    def test_action_exclude_records_exclude(self) -> None:
        comments = [_make_classified()]
        app = CuratorApp(comments=comments, project_name="org/repo")
        app._record_decision = MagicMock()  # type: ignore[method-assign]

        app.action_exclude()

        app._record_decision.assert_called_once_with("exclude")

    def test_action_skip_records_skip(self) -> None:
        comments = [_make_classified()]
        app = CuratorApp(comments=comments, project_name="org/repo")
        app._record_decision = MagicMock()  # type: ignore[method-assign]

        app.action_skip()

        app._record_decision.assert_called_once_with("skip")


class TestCommentView:
    """Test the CommentView widget construction."""

    def test_widget_can_be_instantiated(self) -> None:
        view = CommentView()
        assert view is not None

    def test_widget_with_id(self) -> None:
        view = CommentView(id="comment-view")
        assert view.id == "comment-view"


class TestEditScreen:
    """Test the EditScreen modal construction."""

    def test_instantiation(self) -> None:
        screen = EditScreen(body="Some body text")
        assert screen._body == "Some body text"

    def test_bindings(self) -> None:
        binding_keys = [b[0] for b in EditScreen.BINDINGS]
        assert "escape" in binding_keys
        assert "ctrl+s" in binding_keys


class TestButtonPressed:
    """Test the on_button_pressed dispatcher."""

    def _make_button_event(self, button_id: str) -> MagicMock:
        event = MagicMock()
        event.button.id = button_id
        return event

    def test_include_button(self) -> None:
        app = CuratorApp(comments=[_make_classified()], project_name="org/repo")
        app.action_include = MagicMock()  # type: ignore[method-assign]

        app.on_button_pressed(self._make_button_event("btn-include"))

        app.action_include.assert_called_once()

    def test_exclude_button(self) -> None:
        app = CuratorApp(comments=[_make_classified()], project_name="org/repo")
        app.action_exclude = MagicMock()  # type: ignore[method-assign]

        app.on_button_pressed(self._make_button_event("btn-exclude"))

        app.action_exclude.assert_called_once()

    def test_edit_button(self) -> None:
        app = CuratorApp(comments=[_make_classified()], project_name="org/repo")
        app.action_edit = MagicMock()  # type: ignore[method-assign]

        app.on_button_pressed(self._make_button_event("btn-edit"))

        app.action_edit.assert_called_once()

    def test_skip_button(self) -> None:
        app = CuratorApp(comments=[_make_classified()], project_name="org/repo")
        app.action_skip = MagicMock()  # type: ignore[method-assign]

        app.on_button_pressed(self._make_button_event("btn-skip"))

        app.action_skip.assert_called_once()

    def test_unknown_button_does_nothing(self) -> None:
        app = CuratorApp(comments=[_make_classified()], project_name="org/repo")
        # Should not raise
        app.on_button_pressed(self._make_button_event("btn-unknown"))
