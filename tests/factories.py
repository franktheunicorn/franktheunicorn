"""factory_boy factories for franktheunicorn models."""

from __future__ import annotations

from decimal import Decimal

import factory  # type: ignore[import-untyped]

from franktheunicorn.core.models import (
    AntiPattern,
    CostRecord,
    OperatorAction,
    Project,
    PullRequest,
    ReviewDraft,
    TestRun,
)


class ProjectFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for Project model instances."""

    class Meta:
        model = Project

    owner = factory.Sequence(lambda n: f"org-{n}")
    repo = factory.Sequence(lambda n: f"repo-{n}")
    review_context = "general open-source"
    enabled = True


class PullRequestFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for PullRequest model instances."""

    class Meta:
        model = PullRequest

    project = factory.SubFactory(ProjectFactory)
    github_id = factory.Sequence(lambda n: 1000 + n)
    number = factory.Sequence(lambda n: n + 1)
    title = factory.Faker("sentence", nb_words=6)
    author = factory.Faker("user_name")
    state = "open"
    url = factory.LazyAttribute(
        lambda o: f"https://github.com/{o.project.owner}/{o.project.repo}/pull/{o.number}"
    )
    diff_url = ""
    body = ""
    labels = factory.LazyFunction(list)
    requested_reviewers = factory.LazyFunction(list)
    changed_files = factory.LazyFunction(list)
    additions = 0
    deletions = 0
    interest_score = 0.0
    score_breakdown = factory.LazyFunction(dict)
    is_draft = False
    likely_ai_generated = False
    is_operator_pr = False
    is_new_contributor = False
    is_low_context = False
    is_likely_unowned = False
    queue = "review"


class ReviewDraftFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for ReviewDraft model instances."""

    class Meta:
        model = ReviewDraft

    pull_request = factory.SubFactory(PullRequestFactory)
    file_path = factory.Faker("file_path", extension="py")
    line_number = factory.Faker("random_int", min=1, max=500)
    comment_body = factory.Faker("paragraph")
    suggestion = ""
    confidence = 0.5
    source = "agent"
    category = "other"
    severity = "nit"
    status = "pending"
    edited_body = ""
    backend_used = ""


class AntiPatternFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for AntiPattern model instances."""

    class Meta:
        model = AntiPattern

    pattern_text = factory.Faker("sentence")
    description = factory.Faker("paragraph")
    weight = 1.0
    times_triggered = 0
    is_active = True
    project = None


class OperatorActionFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for OperatorAction model instances."""

    class Meta:
        model = OperatorAction

    action_type = "accept_draft"
    review_draft = None
    pull_request = factory.SubFactory(PullRequestFactory)
    notes = ""


class CostRecordFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for CostRecord model instances."""

    class Meta:
        model = CostRecord

    project = factory.SubFactory(ProjectFactory)
    pull_request = None
    action_type = "review"
    backend = "claude"
    tokens_in = 1000
    tokens_out = 500
    estimated_cost_usd = Decimal("0.0150")
    duration_seconds = 2.5


class TestRunFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for TestRun model instances."""

    class Meta:
        model = TestRun

    pull_request = factory.SubFactory(PullRequestFactory)
    run_type = "pr_branch"
    status = "pending"
    test_scope = factory.LazyFunction(list)
    container_image = "python:3.12-slim"
