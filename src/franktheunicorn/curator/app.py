"""Textual TUI for curating voice dataset from historical comments."""

from __future__ import annotations

import logging
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Static, TextArea

from franktheunicorn.curator.classifier import ClassifiedComment
from franktheunicorn.curator.dataset import CurationDecision, write_dataset

logger = logging.getLogger(__name__)


class CommentView(Static):
    """Widget showing a single comment with diff context."""

    def update_comment(self, comment: ClassifiedComment, index: int, total: int) -> None:
        """Render a classified comment for review."""
        raw = comment.raw
        tone_info = ""
        if comment.tone_flagged:
            flags = ", ".join(comment.tone_flags)
            tone_info = f"\n[bold red]Tone flags: {flags}[/bold red]"

        content = (
            f"[bold]Comment {index + 1} of {total}[/bold]\n"
            f"[dim]Category: {comment.category}{tone_info}[/dim]\n"
            f"[dim]Author: {raw.author} | File: {raw.file_path} | "
            f"PR #{raw.pr_number}[/dim]\n"
            f"[dim]{raw.created_at}[/dim]\n"
            f"[dim]{raw.url}[/dim]\n\n"
            f"[bold]Diff context:[/bold]\n"
            f"```\n{raw.diff_context}\n```\n\n"
            f"[bold]Comment:[/bold]\n{raw.body}"
        )
        self.update(content)


class EditScreen(ModalScreen[str]):
    """Modal screen for editing a comment body."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("ctrl+s", "save", "Save"),
    ]

    def __init__(self, body: str) -> None:
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        yield Static("[bold]Edit comment body (Ctrl+S to save, Escape to cancel)[/bold]")
        yield TextArea(self._body, id="edit-area")

    def action_cancel(self) -> None:
        self.dismiss("")

    def action_save(self) -> None:
        area = self.query_one("#edit-area", TextArea)
        self.dismiss(area.text)


class CuratorApp(App[None]):
    """Interactive TUI for curating voice dataset."""

    TITLE = "franktheunicorn — Comment Curator"

    CSS = """
    CommentView {
        padding: 1 2;
        margin: 1 0;
        border: solid $accent;
        height: auto;
        max-height: 70vh;
        overflow-y: auto;
    }

    #progress {
        padding: 0 2;
        color: $text-muted;
    }

    Horizontal {
        padding: 1 2;
        height: auto;
    }

    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        ("i", "include", "Include"),
        ("e", "exclude", "Exclude"),
        ("d", "edit", "Edit"),
        ("s", "skip", "Skip"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        comments: list[ClassifiedComment],
        project_name: str,
        output_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self.comments = comments
        self.project_name = project_name
        self.output_dir = output_dir
        self.current_index = 0
        self.decisions: list[CurationDecision] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            CommentView(id="comment-view"),
            Horizontal(
                Button("[I]nclude", id="btn-include", variant="success"),
                Button("[E]xclude", id="btn-exclude", variant="error"),
                Button("[D] Edit", id="btn-edit", variant="warning"),
                Button("[S]kip", id="btn-skip"),
            ),
            Static(id="progress"),
        )
        yield Footer()

    def on_mount(self) -> None:
        self._show_current()

    def _show_current(self) -> None:
        """Update the comment view with the current comment."""
        if self.current_index >= len(self.comments):
            self._finish()
            return
        view = self.query_one("#comment-view", CommentView)
        view.update_comment(
            self.comments[self.current_index],
            self.current_index,
            len(self.comments),
        )
        progress = self.query_one("#progress", Static)
        done = len(self.decisions)
        total = len(self.comments)
        progress.update(
            f"Progress: {done}/{total} reviewed | Remaining: {total - self.current_index}"
        )

    def _record_decision(self, decision: str, edited_body: str = "", note: str = "") -> None:
        """Record a curation decision and advance."""
        comment = self.comments[self.current_index]
        self.decisions.append(
            CurationDecision(
                comment=comment,
                decision=decision,
                edited_body=edited_body,
                note=note,
            )
        )
        self._advance()

    def _advance(self) -> None:
        """Move to next comment or finish."""
        self.current_index += 1
        self._show_current()

    def _finish(self) -> None:
        """Write dataset and exit."""
        if self.decisions:
            output_path = write_dataset(self.decisions, self.project_name, self.output_dir)
            included = sum(1 for d in self.decisions if d.decision == "include")
            self.notify(
                f"Done! {included} comments saved to {output_path}",
                title="Curation complete",
            )
        self.exit()

    def action_include(self) -> None:
        if self.current_index < len(self.comments):
            self._record_decision("include")

    def action_exclude(self) -> None:
        if self.current_index < len(self.comments):
            self._record_decision("exclude")

    def action_skip(self) -> None:
        if self.current_index < len(self.comments):
            self._record_decision("skip")

    def action_edit(self) -> None:
        if self.current_index < len(self.comments):
            body = self.comments[self.current_index].raw.body

            def _on_edit_done(edited: str | None) -> None:
                if edited:
                    self._record_decision("include", edited_body=edited)

            self.push_screen(EditScreen(body), callback=_on_edit_done)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "btn-include":
                self.action_include()
            case "btn-exclude":
                self.action_exclude()
            case "btn-edit":
                self.action_edit()
            case "btn-skip":
                self.action_skip()
