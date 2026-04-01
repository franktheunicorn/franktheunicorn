"""Training data export pipeline for fine-tuning (v2 — §10.2).

Exports operator action history into JSONL training data for Axolotl.
Merges voice-curated data with approved/edited/rejected findings.
Classifies comment structure and applies weights for training.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Minimum operator actions required before export is allowed.
MIN_ACTIONS_TO_EXPORT = 200

# Structure classification patterns.
_QUESTION_RE = re.compile(r"\?(\s|$)")
_SUGGESTION_STARTERS = re.compile(
    r"(?i)(consider |you might|you could|try |perhaps|maybe |it would be|"
    r"i'd suggest|one option|an alternative)",
)
_PRAISE_STARTERS = re.compile(
    r"(?i)(great|nice|good|well done|excellent|clean|solid|looks good|"
    r"love this|impressive|smart)",
)
_FIX_STARTERS = re.compile(
    r"(?i)(use |change |replace |rename |fix |update |remove |add |"
    r"should be|needs to be|must be)",
)

# Weights per comment structure type.
STRUCTURE_WEIGHTS: dict[str, float] = {
    "praise-suggestion": 2.0,
    "direct-fix": 2.0,
    "question": 1.5,
    "flag-for-discussion": 1.0,
    "pure-praise": 1.0,
    "pure-critique": 0.5,
    "shepherding-response": 1.5,
    "other": 1.0,
}

# Action types that map to shepherding feedback.
SHEPHERD_ACTION_TYPES = frozenset({"accept_shepherd", "reject_shepherd", "edit_shepherd"})

# All review-related action types (including shepherding).
ALL_FEEDBACK_ACTION_TYPES = frozenset(
    {
        "accept_draft",
        "reject_draft",
        "edit_draft",
        "accept_shepherd",
        "reject_shepherd",
        "edit_shepherd",
    }
)


@dataclass
class ExportResult:
    """Result of a training data export operation."""

    train_count: int = 0
    eval_count: int = 0
    total_actions: int = 0
    skipped_count: int = 0
    structure_counts: dict[str, int] = field(default_factory=dict)
    output_dir: Path | None = None
    error: str = ""


def classify_comment_structure(text: str) -> str:
    """Classify comment structure into a training category.

    Returns one of: praise-suggestion, direct-fix, question,
    flag-for-discussion, pure-praise, pure-critique, other.
    """
    if not text.strip():
        return "other"

    has_praise = bool(_PRAISE_STARTERS.search(text))
    has_suggestion = bool(_SUGGESTION_STARTERS.search(text))
    has_fix = bool(_FIX_STARTERS.search(text))
    has_question = bool(_QUESTION_RE.search(text))

    if has_praise and (has_suggestion or has_fix):
        return "praise-suggestion"
    if has_fix and not has_praise:
        return "direct-fix"
    if has_question and not has_fix:
        return "question"
    if has_praise and not has_suggestion and not has_fix:
        return "pure-praise"
    if has_suggestion:
        return "flag-for-discussion"

    return "other"


def build_instruction_example(
    *,
    instruction_context: str,
    diff_input: str,
    output_text: str,
    weight: float = 1.0,
    finding_id: int | None = None,
    is_dpo: bool = False,
    chosen: str = "",
    rejected: str = "",
) -> dict[str, Any]:
    """Build an Alpaca-format training example.

    For DPO pairs, set is_dpo=True and provide chosen/rejected.
    """
    example: dict[str, Any] = {
        "instruction": instruction_context,
        "input": diff_input,
        "weight": weight,
    }
    if finding_id is not None:
        example["finding_id"] = finding_id

    if is_dpo:
        example["chosen"] = chosen
        example["rejected"] = rejected
    else:
        example["output"] = output_text

    return example


def _build_instruction_context(
    project_name: str,
    review_context: str,
    anti_patterns: list[str],
) -> str:
    """Build the instruction context string for training examples."""
    parts = [
        f"You are reviewing a PR to {project_name}.",
        f"Project context:\n{review_context}",
    ]
    if anti_patterns:
        ap_text = "\n".join(f"- {ap}" for ap in anti_patterns[:10])
        parts.append(f"Anti-patterns to avoid:\n{ap_text}")
    parts.append("Review the following diff and produce review comments.")
    return "\n\n".join(parts)


def _build_diff_input(
    pr_title: str,
    pr_body: str,
    file_path: str,
    code_context: str,
) -> str:
    """Build the diff input string for a training example."""
    parts = [f"PR title: {pr_title}"]
    if pr_body:
        parts.append(f"PR body: {pr_body[:500]}")
    if file_path:
        parts.append(f"File: {file_path}")
    if code_context:
        parts.append(f"Diff:\n```\n{code_context}\n```")
    return "\n".join(parts)


def _load_voice_curated(voice_dir: Path, project_name: str) -> list[dict[str, Any]]:
    """Load voice-curated JSONL for a project if it exists."""
    # Try project subdirectory first, then flat file.
    safe_name = project_name.replace("/", "-")
    candidates = [
        voice_dir / project_name / "voice_curated.jsonl",
        voice_dir / safe_name / "voice_curated.jsonl",
    ]
    for path in candidates:
        if path.exists():
            examples = []
            for line in path.read_text().strip().split("\n"):
                line = line.strip()
                if line:
                    try:
                        examples.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.debug("Skipping invalid JSONL line in %s", path)
            logger.info("Loaded %d voice-curated examples from %s", len(examples), path)
            return examples
    return []


def export_training_data(
    project_id: int,
    output_dir: Path,
    *,
    min_actions: int = MIN_ACTIONS_TO_EXPORT,
    force: bool = False,
    data_dir: Path | None = None,
) -> ExportResult:
    """Export training data for a project to JSONL files.

    Returns an ExportResult with counts and output paths.
    Requires at least ``min_actions`` operator actions unless force=True.
    """
    import django

    django.setup()

    from franktheunicorn.core.models import AntiPattern, OperatorAction, Project

    try:
        project = Project.objects.get(pk=project_id)
    except Project.DoesNotExist:
        return ExportResult(error=f"Project with id={project_id} not found")

    # Query all feedback actions for this project.
    actions = list(
        OperatorAction.objects.filter(
            action_type__in=sorted(ALL_FEEDBACK_ACTION_TYPES),
            review_draft__isnull=False,
            review_draft__pull_request__project_id=project_id,
        )
        .select_related(
            "review_draft",
            "review_draft__pull_request",
        )
        .order_by("created_at")
    )

    if len(actions) < min_actions and not force:
        return ExportResult(
            total_actions=len(actions),
            error=(
                f"Not enough actions to export ({len(actions)} < {min_actions}). "
                f"Use force=True to override."
            ),
        )

    if not actions:
        return ExportResult(total_actions=0, error="No operator actions found")

    # Load anti-patterns for instruction context.
    anti_patterns = list(
        AntiPattern.objects.filter(project=project, is_active=True).values_list(
            "pattern_text", flat=True
        )[:10]
    )

    # Load voice-curated data.
    voice_examples: list[dict[str, Any]] = []
    if data_dir is not None:
        voice_dir = data_dir / "voice"
        voice_examples = _load_voice_curated(voice_dir, project.full_name)

    instruction_context = _build_instruction_context(
        project.full_name,
        project.review_context,
        list(anti_patterns),
    )

    # Build training examples from actions.
    examples: list[dict[str, Any]] = []
    seen_finding_ids: set[int] = set()
    structure_counts: dict[str, int] = {}
    skipped = 0

    for action in actions:
        draft = action.review_draft
        if draft is None:
            skipped += 1
            continue

        # Deduplicate by finding ID.
        if draft.pk in seen_finding_ids:
            skipped += 1
            continue
        seen_finding_ids.add(draft.pk)

        pr = draft.pull_request
        diff_input = _build_diff_input(
            pr.title,
            pr.body,
            draft.file_path,
            draft.code_context,
        )

        is_shepherd = action.action_type in SHEPHERD_ACTION_TYPES

        if action.action_type in ("edit_draft", "edit_shepherd"):
            # DPO pair: original is rejected, edited is chosen.
            edited_text = draft.edited_body or draft.comment_body
            original_text = draft.comment_body

            if edited_text != original_text:
                structure = (
                    "shepherding-response"
                    if is_shepherd
                    else classify_comment_structure(edited_text)
                )
                weight = STRUCTURE_WEIGHTS.get(structure, 1.0)
                structure_counts[structure] = structure_counts.get(structure, 0) + 1

                example = build_instruction_example(
                    instruction_context=instruction_context,
                    diff_input=diff_input,
                    output_text="",
                    weight=weight,
                    finding_id=draft.pk,
                    is_dpo=True,
                    chosen=edited_text,
                    rejected=original_text,
                )
                examples.append(example)
            else:
                # Edit with no actual change — treat as acceptance.
                structure = (
                    "shepherding-response"
                    if is_shepherd
                    else classify_comment_structure(draft.comment_body)
                )
                weight = STRUCTURE_WEIGHTS.get(structure, 1.0)
                structure_counts[structure] = structure_counts.get(structure, 0) + 1

                example = build_instruction_example(
                    instruction_context=instruction_context,
                    diff_input=diff_input,
                    output_text=draft.comment_body,
                    weight=weight,
                    finding_id=draft.pk,
                )
                examples.append(example)

        elif action.action_type in ("accept_draft", "accept_shepherd"):
            structure = (
                "shepherding-response"
                if is_shepherd
                else classify_comment_structure(draft.comment_body)
            )
            weight = STRUCTURE_WEIGHTS.get(structure, 1.0)
            structure_counts[structure] = structure_counts.get(structure, 0) + 1

            example = build_instruction_example(
                instruction_context=instruction_context,
                diff_input=diff_input,
                output_text=draft.comment_body,
                weight=weight,
                finding_id=draft.pk,
            )
            examples.append(example)

        elif action.action_type in ("reject_draft", "reject_shepherd"):
            # Rejected findings are negative examples — lower weight.
            structure = "pure-critique"
            weight = STRUCTURE_WEIGHTS.get(structure, 0.5)
            structure_counts[structure] = structure_counts.get(structure, 0) + 1

            # Include as a negative example with the rejection reason if available.
            output = (
                f"[REJECTED] {action.notes}" if action.notes else f"[REJECTED] {draft.comment_body}"
            )
            example = build_instruction_example(
                instruction_context=instruction_context,
                diff_input=diff_input,
                output_text=output,
                weight=weight,
                finding_id=draft.pk,
            )
            examples.append(example)

    # Add voice-curated examples (already in instruction format).
    for ve in voice_examples:
        if "instruction" in ve and "output" in ve and ve.get("finding_id") not in seen_finding_ids:
            ve.setdefault("weight", 1.0)
            examples.append(ve)

    if not examples:
        return ExportResult(
            total_actions=len(actions),
            skipped_count=skipped,
            error="No training examples generated",
        )

    # Split train/eval (80/20).
    split_idx = max(1, int(len(examples) * 0.8))
    train_examples = examples[:split_idx]
    eval_examples = examples[split_idx:]

    # Write output files.
    output_dir.mkdir(parents=True, exist_ok=True)

    train_path = output_dir / "train.jsonl"
    with open(train_path, "w") as f:
        for ex in train_examples:
            f.write(json.dumps(ex) + "\n")

    eval_path = output_dir / "eval.jsonl"
    with open(eval_path, "w") as f:
        for ex in eval_examples:
            f.write(json.dumps(ex) + "\n")

    metadata = {
        "project": project.full_name,
        "project_id": project_id,
        "total_actions": len(actions),
        "total_examples": len(examples),
        "train_count": len(train_examples),
        "eval_count": len(eval_examples),
        "skipped_count": skipped,
        "structure_counts": structure_counts,
        "voice_curated_count": len(voice_examples),
    }
    metadata_path = output_dir / "metadata.json"
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    logger.info(
        "Exported %d training examples (%d train, %d eval) to %s",
        len(examples),
        len(train_examples),
        len(eval_examples),
        output_dir,
    )

    return ExportResult(
        train_count=len(train_examples),
        eval_count=len(eval_examples),
        total_actions=len(actions),
        skipped_count=skipped,
        structure_counts=structure_counts,
        output_dir=output_dir,
    )
