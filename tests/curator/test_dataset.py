"""Tests for the dataset writer."""

from __future__ import annotations

import json
from pathlib import Path

from franktheunicorn.curator.classifier import ClassifiedComment
from franktheunicorn.curator.dataset import CurationDecision, write_dataset
from franktheunicorn.curator.scraper import RawComment


def _make_raw(body: str = "Fix this bug", **kwargs) -> RawComment:
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
    body: str = "Fix this bug", category: str = "correctness"
) -> ClassifiedComment:
    return ClassifiedComment(
        raw=_make_raw(body=body),
        category=category,
        tone_flagged=False,
        tone_flags=[],
    )


def _make_decision(
    body: str = "Fix this bug",
    decision: str = "include",
    edited_body: str = "",
    note: str = "",
    category: str = "correctness",
) -> CurationDecision:
    return CurationDecision(
        comment=_make_classified(body=body, category=category),
        decision=decision,
        edited_body=edited_body,
        note=note,
    )


class TestWriteDataset:
    def test_writes_included_comments(self, tmp_path: Path) -> None:
        decisions = [
            _make_decision(body="Fix the null check", decision="include"),
            _make_decision(body="Style nit", decision="exclude"),
            _make_decision(body="Add test", decision="include", category="test-coverage"),
        ]

        output = write_dataset(decisions, "org/repo", output_dir=tmp_path)

        assert output.exists()
        lines = output.read_text().strip().split("\n")
        assert len(lines) == 2  # Only included

        record1 = json.loads(lines[0])
        assert record1["body"] == "Fix the null check"
        assert record1["category"] == "correctness"
        assert record1["edited"] is False

        record2 = json.loads(lines[1])
        assert record2["body"] == "Add test"
        assert record2["category"] == "test-coverage"

    def test_uses_edited_body_when_present(self, tmp_path: Path) -> None:
        decisions = [
            _make_decision(
                body="Original text",
                decision="include",
                edited_body="Improved text",
            ),
        ]

        output = write_dataset(decisions, "org/repo", output_dir=tmp_path)

        record = json.loads(output.read_text().strip())
        assert record["body"] == "Improved text"
        assert record["original_body"] == "Original text"
        assert record["edited"] is True

    def test_creates_project_directory(self, tmp_path: Path) -> None:
        decisions = [_make_decision(decision="include")]

        output = write_dataset(decisions, "apache/spark", output_dir=tmp_path)

        assert output.parent == tmp_path / "apache" / "spark"
        assert output.name == "voice_curated.jsonl"

    def test_appends_to_existing_file(self, tmp_path: Path) -> None:
        decisions1 = [_make_decision(body="First", decision="include")]
        decisions2 = [_make_decision(body="Second", decision="include")]

        output = write_dataset(decisions1, "org/repo", output_dir=tmp_path)
        write_dataset(decisions2, "org/repo", output_dir=tmp_path)

        lines = output.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["body"] == "First"
        assert json.loads(lines[1])["body"] == "Second"

    def test_skipped_and_excluded_not_written(self, tmp_path: Path) -> None:
        decisions = [
            _make_decision(decision="exclude"),
            _make_decision(decision="skip"),
        ]

        output = write_dataset(decisions, "org/repo", output_dir=tmp_path)

        # File is created but empty (opened in append mode, nothing written)
        assert output.read_text() == ""

    def test_empty_decisions_list(self, tmp_path: Path) -> None:
        output = write_dataset([], "org/repo", output_dir=tmp_path)

        assert output.exists()
        assert output.read_text() == ""

    def test_note_preserved(self, tmp_path: Path) -> None:
        decisions = [
            _make_decision(decision="include", note="Great example of tone"),
        ]

        output = write_dataset(decisions, "org/repo", output_dir=tmp_path)

        record = json.loads(output.read_text().strip())
        assert record["note"] == "Great example of tone"

    def test_tone_flags_preserved(self, tmp_path: Path) -> None:
        classified = ClassifiedComment(
            raw=_make_raw(),
            category="correctness",
            tone_flagged=True,
            tone_flags=["abrasive", "snarky"],
        )
        decisions = [
            CurationDecision(
                comment=classified,
                decision="include",
                edited_body="",
                note="",
            )
        ]

        output = write_dataset(decisions, "org/repo", output_dir=tmp_path)

        record = json.loads(output.read_text().strip())
        assert record["tone_flagged"] is True
        assert record["tone_flags"] == ["abrasive", "snarky"]
