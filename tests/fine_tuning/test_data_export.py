"""Tests for fine-tuning training data export pipeline (v2 — §10.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from franktheunicorn.fine_tuning.data_export import (
    STRUCTURE_WEIGHTS,
    build_instruction_example,
    classify_comment_structure,
    export_training_data,
)
from tests.factories import (
    OperatorActionFactory,
    ProjectFactory,
    PullRequestFactory,
    ReviewDraftFactory,
)


class TestClassifyCommentStructure:
    def test_praise_suggestion(self) -> None:
        text = "Great structure here. Consider renaming processData for clarity."
        assert classify_comment_structure(text) == "praise-suggestion"

    def test_praise_with_fix(self) -> None:
        text = "Nice work! Use isNullAt(idx) instead of == null."
        assert classify_comment_structure(text) == "praise-suggestion"

    def test_direct_fix(self) -> None:
        text = "Use isNullAt(idx) instead of == null to handle Spark's null."
        assert classify_comment_structure(text) == "direct-fix"

    def test_question(self) -> None:
        text = "Did you verify this works with the Connect protocol?"
        assert classify_comment_structure(text) == "question"

    def test_pure_praise(self) -> None:
        text = "Looks good to me, clean implementation."
        assert classify_comment_structure(text) == "pure-praise"

    def test_suggestion_flag(self) -> None:
        text = "Consider adding a test for the edge case with empty input."
        assert classify_comment_structure(text) == "flag-for-discussion"

    def test_other(self) -> None:
        text = "This code handles the serialization path."
        assert classify_comment_structure(text) == "other"

    def test_empty_text(self) -> None:
        assert classify_comment_structure("") == "other"
        assert classify_comment_structure("   ") == "other"


class TestBuildInstructionExample:
    def test_standard_example(self) -> None:
        example = build_instruction_example(
            instruction_context="Review this PR.",
            diff_input="File: foo.py\nDiff: ...",
            output_text="Use isNullAt() instead.",
            weight=2.0,
            finding_id=42,
        )
        assert example["instruction"] == "Review this PR."
        assert example["input"] == "File: foo.py\nDiff: ..."
        assert example["output"] == "Use isNullAt() instead."
        assert example["weight"] == 2.0
        assert example["finding_id"] == 42
        assert "chosen" not in example
        assert "rejected" not in example

    def test_dpo_example(self) -> None:
        example = build_instruction_example(
            instruction_context="Review this PR.",
            diff_input="File: foo.py",
            output_text="",
            is_dpo=True,
            chosen="Good suggestion with context.",
            rejected="This is wrong, fix it.",
        )
        assert example["chosen"] == "Good suggestion with context."
        assert example["rejected"] == "This is wrong, fix it."
        assert "output" not in example

    def test_default_weight(self) -> None:
        example = build_instruction_example(
            instruction_context="ctx",
            diff_input="input",
            output_text="output",
        )
        assert example["weight"] == 1.0


class TestStructureWeights:
    def test_praise_suggestion_weighted_high(self) -> None:
        assert STRUCTURE_WEIGHTS["praise-suggestion"] == 2.0

    def test_direct_fix_weighted_high(self) -> None:
        assert STRUCTURE_WEIGHTS["direct-fix"] == 2.0

    def test_shepherding_response_weighted(self) -> None:
        assert STRUCTURE_WEIGHTS["shepherding-response"] == 1.5

    def test_pure_critique_weighted_low(self) -> None:
        assert STRUCTURE_WEIGHTS["pure-critique"] == 0.5


@pytest.mark.django_db
class TestExportTrainingData:
    def _create_actions(
        self,
        project: object,
        count: int,
        action_type: str = "accept_draft",
        *,
        source: str = "agent",
    ) -> list[object]:
        """Create ``count`` operator actions with review drafts for testing."""
        actions = []
        for _ in range(count):
            pr = PullRequestFactory(project=project)
            draft = ReviewDraftFactory(
                pull_request=pr,
                comment_body="Great structure. Consider adding a test.",
                code_context="+ def new_function():\n+     pass",
                sources=[source],
            )
            action = OperatorActionFactory(
                action_type=action_type,
                review_draft=draft,
                pull_request=pr,
            )
            actions.append(action)
        return actions

    def test_export_insufficient_actions(self, tmp_path: Path) -> None:
        project = ProjectFactory()
        self._create_actions(project, 10)

        result = export_training_data(project.pk, tmp_path / "output")
        assert result.error
        assert "Not enough actions" in result.error
        assert result.total_actions == 10

    def test_export_with_force(self, tmp_path: Path) -> None:
        project = ProjectFactory()
        self._create_actions(project, 5)

        output_dir = tmp_path / "output"
        result = export_training_data(project.pk, output_dir, force=True)
        assert result.error == ""
        assert result.train_count > 0
        assert result.eval_count >= 0
        assert result.total_actions == 5
        assert (output_dir / "train.jsonl").exists()
        assert (output_dir / "eval.jsonl").exists()
        assert (output_dir / "metadata.json").exists()

    def test_export_creates_valid_jsonl(self, tmp_path: Path) -> None:
        project = ProjectFactory()
        self._create_actions(project, 10)

        output_dir = tmp_path / "output"
        export_training_data(project.pk, output_dir, force=True)

        train_lines = (output_dir / "train.jsonl").read_text().strip().split("\n")
        for line in train_lines:
            example = json.loads(line)
            assert "instruction" in example
            assert "input" in example
            assert "weight" in example
            # Either output or chosen/rejected must be present.
            assert "output" in example or "chosen" in example

    def test_export_metadata_correct(self, tmp_path: Path) -> None:
        project = ProjectFactory()
        self._create_actions(project, 10)

        output_dir = tmp_path / "output"
        export_training_data(project.pk, output_dir, force=True)

        metadata = json.loads((output_dir / "metadata.json").read_text())
        assert metadata["project"] == project.full_name
        assert metadata["total_actions"] == 10
        assert metadata["train_count"] + metadata["eval_count"] == metadata["total_examples"]

    def test_export_train_eval_split(self, tmp_path: Path) -> None:
        project = ProjectFactory()
        self._create_actions(project, 20)

        output_dir = tmp_path / "output"
        result = export_training_data(project.pk, output_dir, force=True)

        # 80/20 split.
        total = result.train_count + result.eval_count
        assert result.train_count == int(total * 0.8) or result.train_count == max(
            1, int(total * 0.8)
        )

    def test_export_deduplicates_by_finding_id(self, tmp_path: Path) -> None:
        project = ProjectFactory()
        pr = PullRequestFactory(project=project)
        draft = ReviewDraftFactory(pull_request=pr, comment_body="Test comment.")

        # Create two actions for the same draft.
        OperatorActionFactory(action_type="accept_draft", review_draft=draft, pull_request=pr)
        OperatorActionFactory(action_type="reject_draft", review_draft=draft, pull_request=pr)

        output_dir = tmp_path / "output"
        result = export_training_data(project.pk, output_dir, force=True)

        # Only one example should be created (first action wins).
        assert result.train_count + result.eval_count == 1

    def test_export_edited_draft_creates_dpo(self, tmp_path: Path) -> None:
        project = ProjectFactory()
        pr = PullRequestFactory(project=project)
        draft = ReviewDraftFactory(
            pull_request=pr,
            comment_body="Original comment.",
            edited_body="Improved comment with context.",
            status="edited",
        )
        OperatorActionFactory(action_type="edit_draft", review_draft=draft, pull_request=pr)

        output_dir = tmp_path / "output"
        export_training_data(project.pk, output_dir, force=True)

        # The SFT dataset (alpaca format) gets the edited text as output...
        train_lines = (output_dir / "train.jsonl").read_text().strip().split("\n")
        example = json.loads(train_lines[0])
        assert example.get("output") == "Improved comment with context."
        assert "chosen" not in example  # DPO rows are malformed for alpaca
        # ...and the preference pair goes to the separate DPO file.
        dpo_lines = (output_dir / "dpo.jsonl").read_text().strip().split("\n")
        pair = json.loads(dpo_lines[0])
        assert pair.get("chosen") == "Improved comment with context."
        assert pair.get("rejected") == "Original comment."

    def test_export_rejected_draft_is_negative(self, tmp_path: Path) -> None:
        project = ProjectFactory()
        pr = PullRequestFactory(project=project)
        draft = ReviewDraftFactory(
            pull_request=pr,
            comment_body="Bad comment.",
        )
        OperatorActionFactory(
            action_type="reject_draft",
            review_draft=draft,
            pull_request=pr,
            notes="Too pedantic",
        )

        output_dir = tmp_path / "output"
        export_training_data(project.pk, output_dir, force=True)

        train_lines = (output_dir / "train.jsonl").read_text().strip().split("\n")
        example = json.loads(train_lines[0])
        assert "[REJECTED]" in example["output"]
        assert "Too pedantic" in example["output"]

    def test_export_includes_shepherd_actions(self, tmp_path: Path) -> None:
        project = ProjectFactory()
        pr = PullRequestFactory(project=project, is_operator_pr=True)
        draft = ReviewDraftFactory(
            pull_request=pr,
            comment_body="I'll address this in the next commit.",
            sources=["shepherding"],
        )
        OperatorActionFactory(
            action_type="accept_shepherd",
            review_draft=draft,
            pull_request=pr,
        )

        output_dir = tmp_path / "output"
        result = export_training_data(project.pk, output_dir, force=True)

        assert result.train_count + result.eval_count >= 1
        assert "shepherding-response" in result.structure_counts

    def test_export_shepherd_edit_creates_dpo(self, tmp_path: Path) -> None:
        project = ProjectFactory()
        pr = PullRequestFactory(project=project, is_operator_pr=True)
        draft = ReviewDraftFactory(
            pull_request=pr,
            comment_body="Auto-generated response.",
            edited_body="Actually, I think the approach in the next PR is better.",
            sources=["shepherding"],
        )
        OperatorActionFactory(
            action_type="edit_shepherd",
            review_draft=draft,
            pull_request=pr,
        )

        output_dir = tmp_path / "output"
        export_training_data(project.pk, output_dir, force=True)

        train_lines = (output_dir / "train.jsonl").read_text().strip().split("\n")
        example = json.loads(train_lines[0])
        assert example.get("output") == "Actually, I think the approach in the next PR is better."
        dpo_lines = (output_dir / "dpo.jsonl").read_text().strip().split("\n")
        pair = json.loads(dpo_lines[0])
        assert pair.get("chosen") == "Actually, I think the approach in the next PR is better."
        assert pair.get("rejected") == "Auto-generated response."

    def test_export_nonexistent_project(self, tmp_path: Path) -> None:
        result = export_training_data(99999, tmp_path / "output")
        assert "not found" in result.error

    def test_export_loads_voice_curated(self, tmp_path: Path) -> None:
        project = ProjectFactory()
        self._create_actions(project, 5)

        # Create voice curated data.
        data_dir = tmp_path / "data"
        voice_dir = data_dir / "voice" / project.full_name
        voice_dir.mkdir(parents=True)
        voice_file = voice_dir / "voice_curated.jsonl"
        voice_example = {
            "instruction": "Review this code.",
            "input": "File: test.py",
            "output": "Consider adding error handling.",
        }
        voice_file.write_text(json.dumps(voice_example) + "\n")

        output_dir = tmp_path / "output"
        result = export_training_data(project.pk, output_dir, force=True, data_dir=data_dir)

        # Should include voice curated examples plus action-based examples.
        assert result.train_count + result.eval_count >= 6  # 5 actions + 1 voice

    def test_export_transforms_real_curator_records(self, tmp_path: Path) -> None:
        """Records in the actual curator schema (curator/dataset.py) must be
        transformed into instruction format, not silently dropped — that
        schema mismatch previously discarded every curated comment."""
        project = ProjectFactory()
        self._create_actions(project, 5)

        data_dir = tmp_path / "data"
        voice_dir = data_dir / "voice" / project.full_name
        voice_dir.mkdir(parents=True)
        curator_record = {
            "author": "holdenk",
            "body": "Use isNullAt(idx) here — == null misses SQL nulls.",
            "original_body": "Use isNullAt.",
            "diff_context": "+ if (value == null) {",
            "file_path": "core/RDD.scala",
            "pr_number": 42,
            "pr_title": "Fix null handling",
            "created_at": "2026-01-01T00:00:00Z",
            "url": "https://github.com/apache/spark/pull/42",
            "category": "code-style",
            "tone_flagged": False,
            "tone_flags": [],
            "edited": True,
            "note": "",
        }
        (voice_dir / "voice_curated.jsonl").write_text(json.dumps(curator_record) + "\n")

        output_dir = tmp_path / "output"
        result = export_training_data(project.pk, output_dir, force=True, data_dir=data_dir)

        assert result.train_count + result.eval_count >= 6  # 5 actions + 1 voice
        all_rows = [
            json.loads(line)
            for path in (output_dir / "train.jsonl", output_dir / "eval.jsonl")
            for line in path.read_text().strip().split("\n")
            if line
        ]
        voice_rows = [r for r in all_rows if r.get("output", "").startswith("Use isNullAt(idx)")]
        assert len(voice_rows) == 1
        assert "instruction" in voice_rows[0]
        assert "core/RDD.scala" in voice_rows[0]["input"]

    def test_export_structure_counts_tracked(self, tmp_path: Path) -> None:
        project = ProjectFactory()

        # Create actions with different comment structures.
        pr1 = PullRequestFactory(project=project)
        draft1 = ReviewDraftFactory(
            pull_request=pr1,
            comment_body="Great structure. Consider renaming processData.",
        )
        OperatorActionFactory(action_type="accept_draft", review_draft=draft1, pull_request=pr1)

        pr2 = PullRequestFactory(project=project)
        draft2 = ReviewDraftFactory(
            pull_request=pr2,
            comment_body="Use isNullAt() instead of == null.",
        )
        OperatorActionFactory(action_type="accept_draft", review_draft=draft2, pull_request=pr2)

        output_dir = tmp_path / "output"
        result = export_training_data(project.pk, output_dir, force=True)

        assert len(result.structure_counts) > 0
        assert sum(result.structure_counts.values()) >= 2
