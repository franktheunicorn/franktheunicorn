"""Write curated voice datasets to JSONL files."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from franktheunicorn.curator.classifier import ClassifiedComment

logger = logging.getLogger(__name__)

DEFAULT_VOICE_DIR = Path.home() / ".review-agent" / "voice"


@dataclass
class CurationDecision:
    """A curation decision for a single comment."""

    comment: ClassifiedComment
    decision: str  # "include", "exclude", "skip"
    edited_body: str  # empty if not edited
    note: str  # optional operator note


def write_dataset(
    decisions: list[CurationDecision],
    project_name: str,
    output_dir: Path | None = None,
) -> Path:
    """Write curated dataset to JSONL file.

    Output: ``~/.review-agent/voice/{project}/voice_curated.jsonl``

    Only decisions with ``decision == "include"`` are written.
    Returns the path to the written file.
    """
    base_dir = output_dir or DEFAULT_VOICE_DIR
    project_dir = base_dir / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    output_path = project_dir / "voice_curated.jsonl"

    included = [d for d in decisions if d.decision == "include"]

    with output_path.open("a", encoding="utf-8") as f:
        for decision in included:
            raw = decision.comment.raw
            record = {
                "author": raw.author,
                "body": decision.edited_body or raw.body,
                "original_body": raw.body,
                "diff_context": raw.diff_context,
                "file_path": raw.file_path,
                "pr_number": raw.pr_number,
                "pr_title": raw.pr_title,
                "created_at": raw.created_at,
                "url": raw.url,
                "category": decision.comment.category,
                "tone_flagged": decision.comment.tone_flagged,
                "tone_flags": decision.comment.tone_flags,
                "edited": bool(decision.edited_body),
                "note": decision.note,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.info(
        "Wrote %d included comments to %s (of %d total decisions)",
        len(included),
        output_path,
        len(decisions),
    )
    return output_path
