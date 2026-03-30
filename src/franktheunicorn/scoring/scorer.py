"""Interest scoring orchestrator. Pure core + thin Django wrapper."""

from __future__ import annotations

from typing import TYPE_CHECKING

from franktheunicorn.scoring.blame import score_blame_proximity
from franktheunicorn.scoring.collaborators import score_collaborator
from franktheunicorn.scoring.sandbox import evaluate_custom_score
from franktheunicorn.scoring.signals import (
    score_ai_generated,
    score_frequent_contributor,
    score_large_pr,
    score_new_contributor,
    score_operator_is_author,
    score_path_overlap,
    score_review_requested,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.core.models import PullRequest


def _get_list(d: dict[str, object], key: str) -> list[str]:
    """Extract a string list from a dict, tolerating None/missing."""
    val = d.get(key)
    return list(val) if val else []  # type: ignore[arg-type]


def score_pull_request(
    pr_dict: dict[str, object],
    project_config_dict: dict[str, object],
    operator_username: str,
    *,
    known_authors: list[str] | None = None,
    blame_data: list[dict[str, object]] | None = None,
    review_history: list[dict[str, str]] | None = None,
    custom_expressions: list[str] | None = None,
) -> tuple[float, dict[str, float]]:
    """Score a PR for operator interest. Pure function — no Django imports.

    Returns (score, breakdown) where score is clamped to [0.0, 1.0].
    """
    breakdown: dict[str, float] = {}
    author = str(pr_dict.get("author", ""))
    changed_files = _get_list(pr_dict, "changed_files")
    reviewers = _get_list(pr_dict, "requested_reviewers")
    additions = int(pr_dict.get("additions", 0) or 0)
    deletions = int(pr_dict.get("deletions", 0) or 0)
    watched = _get_list(project_config_dict, "watched_paths")
    contributors = _get_list(project_config_dict, "frequent_contributors")

    def _add(name: str, value: float | None) -> None:
        if value is not None:
            breakdown[name] = value

    _add("operator_is_author", score_operator_is_author(author, operator_username))
    _add("review_requested", score_review_requested(reviewers, operator_username))
    _add("path_overlap", score_path_overlap(changed_files, watched))
    _add("frequent_contributor", score_frequent_contributor(author, contributors))
    _add(
        "new_contributor",
        score_new_contributor(
            author,
            operator_username,
            contributors,
            known_authors or [],
        ),
    )
    _add("ai_generated_penalty", score_ai_generated(author))
    _add("large_pr_penalty", score_large_pr(additions, deletions))

    if blame_data is not None:
        _add("blame_proximity", score_blame_proximity(blame_data, operator_username))
    if review_history is not None:
        _add("collaborator", score_collaborator(author, operator_username, review_history))
    if custom_expressions:
        for i, expr in enumerate(custom_expressions):
            result = evaluate_custom_score(expr, pr_dict, project_config_dict)
            if result is not None:
                _add(f"custom_{i}", result)

    score = round(max(0.0, min(1.0, sum(breakdown.values()))), 4)
    return score, breakdown


def score_pull_request_from_model(
    pr: PullRequest,
    project_config: ProjectConfig,
    operator_username: str,
    *,
    known_authors: list[str] | None = None,
    blame_data: list[dict[str, object]] | None = None,
    review_history: list[dict[str, str]] | None = None,
    custom_expressions: list[str] | None = None,
) -> tuple[float, dict[str, float]]:
    """Django-aware wrapper: converts models to dicts, resolves known_authors."""
    pr_dict: dict[str, object] = {
        "author": pr.author,
        "requested_reviewers": pr.requested_reviewers,
        "changed_files": pr.changed_files,
        "additions": pr.additions,
        "deletions": pr.deletions,
        "labels": pr.labels,
        "is_draft": pr.is_draft,
        "title": pr.title,
        "body": pr.body,
    }
    config_dict: dict[str, object] = {
        "watched_paths": project_config.watched_paths,
        "frequent_contributors": project_config.frequent_contributors,
    }

    if known_authors is None:
        from franktheunicorn.core.models import PullRequest as PRModel

        known_authors = list(
            PRModel.objects.filter(project=pr.project)
            .exclude(pk=pr.pk)
            .values_list("author", flat=True)
            .distinct()
        )

    return score_pull_request(
        pr_dict,
        config_dict,
        operator_username,
        known_authors=known_authors,
        blame_data=blame_data,
        review_history=review_history,
        custom_expressions=custom_expressions,
    )
