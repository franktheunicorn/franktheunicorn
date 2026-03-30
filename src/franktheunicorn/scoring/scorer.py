"""Interest scoring orchestrator (§2.1). Pure core + thin Django wrapper."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from franktheunicorn.scoring.blame import score_touches_operator_code
from franktheunicorn.scoring.collaborators import compute_collaborator_score
from franktheunicorn.scoring.sandbox import evaluate_custom_score
from franktheunicorn.scoring.signals import (
    MAX_SCORE,
    WEIGHTS,
    score_ai_generated,
    score_has_review_request,
    score_keyword_match,
    score_llm_interest,
    score_mentioned_or_assigned,
    score_new_human_contributor,
    score_path_overlap,
    score_prior_review_history,
)

if TYPE_CHECKING:
    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.core.models import PullRequest


def _get_list(d: dict[str, object], key: str) -> list[str]:
    val = d.get(key)
    if not val:
        return []
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        return [str(x) for x in val]
    return []


def score_pull_request(
    pr_dict: dict[str, object],
    project_config_dict: dict[str, object],
    operator_username: str,
    *,
    known_authors: list[str] | None = None,
    blame_data: list[dict[str, object]] | None = None,
    review_history: list[dict[str, str]] | None = None,
    custom_expressions: list[str] | None = None,
    collaborator_scores: dict[str, float | None] | None = None,
) -> tuple[float, dict[str, float]]:
    """Score a PR for operator interest. Pure function — no Django imports.

    Returns (score, breakdown) where score is normalized to [0.0, 1.0].
    Breakdown values are raw integer points.
    """
    breakdown: dict[str, float] = {}
    author = str(pr_dict.get("author", ""))
    title = str(pr_dict.get("title", "") or "")
    body = str(pr_dict.get("body", "") or "")
    changed_files = _get_list(pr_dict, "changed_files")
    reviewers = _get_list(pr_dict, "requested_reviewers")
    assignees = _get_list(pr_dict, "assignees")
    ai_agents = _get_list(project_config_dict, "ai_agents")
    watched = _get_list(project_config_dict, "watched_paths")
    contributors = _get_list(project_config_dict, "frequent_contributors")
    keywords = _get_list(project_config_dict, "watch_keywords")

    # Per-project weight overrides
    weights = dict(WEIGHTS)
    overrides = project_config_dict.get("scoring_weights")
    if overrides and isinstance(overrides, dict):
        weights.update({k: round(float(v)) for k, v in overrides.items()})

    def _add(name: str, value: int | float | None) -> None:
        if value is None:
            return
        # If this signal has a weight override and returned the default weight,
        # substitute the overridden weight (preserving fractional scaling).
        if name in weights and name in WEIGHTS and WEIGHTS[name] != 0:
            default_w = WEIGHTS[name]
            override_w = weights[name]
            if default_w != override_w:
                value = value * (override_w / default_w)
        breakdown[name] = float(value)

    _add("path_overlap", score_path_overlap(changed_files, watched))
    _add("mentioned_or_assigned", score_mentioned_or_assigned(body, assignees, operator_username))
    _add("has_review_request", score_has_review_request(reviewers, operator_username))
    _add(
        "new_human_contributor",
        score_new_human_contributor(
            author,
            operator_username,
            known_authors or [],
            ai_agents or None,
        ),
    )
    _add("keyword_match", score_keyword_match(title, body, keywords))
    _add("ai_generated", score_ai_generated(author, ai_agents or None))
    _add("llm_interest", score_llm_interest(pr_dict.get("llm_interest")))  # type: ignore[arg-type]

    # Collaborator + review history signals
    _add(
        "collaborator",
        compute_collaborator_score(
            author, operator_username, review_history or [], contributors, collaborator_scores
        ),
    )
    if review_history is not None:
        _add(
            "prior_review_history",
            score_prior_review_history(author, operator_username, review_history),
        )

    if blame_data is not None:
        _add(
            "touches_operator_code",
            score_touches_operator_code(
                blame_data,
                operator_username,
            ),
        )

    if custom_expressions:
        for i, expr in enumerate(custom_expressions):
            result = evaluate_custom_score(expr, pr_dict, project_config_dict)
            if result is not None:
                raw_boost = project_config_dict.get("custom_scoring_max_boost", 30)
                max_boost = int(raw_boost) if isinstance(raw_boost, (int, float)) else 30
                _add(f"custom_{i}", round(result * max_boost))

    raw = sum(breakdown.values())
    score = round(max(0.0, min(1.0, raw / MAX_SCORE)), 4)
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
    collaborator_scores: dict[str, float | None] | None = None,
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
        "assignees": pr.assignees,
    }
    if pr.github_created_at:
        age = (datetime.now(tz=UTC) - pr.github_created_at).days
        pr_dict["pr_age_days"] = age

    config_dict: dict[str, object] = {
        "watched_paths": project_config.watched_paths,
        "frequent_contributors": project_config.frequent_contributors,
        "watch_keywords": project_config.watch_keywords,
        "ai_agents": project_config.ai_agents,
        "scoring_weights": project_config.scoring_weights,
        "custom_scoring_max_boost": project_config.custom_scoring_max_boost,
    }

    if known_authors is None:
        from franktheunicorn.core.models import PullRequest as PRModel

        known_authors = list(
            PRModel.objects.filter(project=pr.project)
            .exclude(pk=pr.pk)
            .values_list("author", flat=True)
            .distinct()
        )

    if collaborator_scores is None:
        collaborator_scores = project_config.collaborator_scores or None

    return score_pull_request(
        pr_dict,
        config_dict,
        operator_username,
        known_authors=known_authors,
        blame_data=blame_data,
        review_history=review_history,
        custom_expressions=custom_expressions or project_config.custom_scoring_expressions,
        collaborator_scores=collaborator_scores,
    )
