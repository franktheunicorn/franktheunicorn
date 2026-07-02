"""factory_boy factories for franktheunicorn models."""

from __future__ import annotations

from decimal import Decimal

import factory  # type: ignore[import-untyped]

from franktheunicorn.core.models import (
    AgentFeedback,
    AntiPattern,
    CostRecord,
    DependencyChange,
    LLMBackendFallback,
    OperatorAction,
    Project,
    PullRequest,
    ReviewDraft,
    SecurityReport,
    TestRun,
)


class ProjectFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for Project model instances."""

    class Meta:
        model = Project

    owner = factory.Sequence(lambda n: f"org-{n}")
    repo = factory.Sequence(lambda n: f"repo-{n}")
    review_context = "general open-source"
    name = factory.LazyAttribute(lambda o: f"{o.owner}-{o.repo}")
    project_type = "personal"
    config_yaml = ""
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
    has_test_coverage = None
    ai_agent_source = ""
    agent_session_url = ""
    agent_task_id = ""
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
    sources = factory.LazyFunction(lambda: ["agent"])
    category = "other"
    severity = "nit"
    rejection_probability = None
    is_auto_suppressed = False
    code_context = ""
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


class DependencyChangeFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for DependencyChange model instances."""

    class Meta:
        model = DependencyChange

    pull_request = factory.SubFactory(PullRequestFactory)
    package_name = factory.Sequence(lambda n: f"package-{n}")
    ecosystem = "python"
    old_version = "1.0.0"
    new_version = "2.0.0"
    source_file = "requirements.txt"
    changelog_url = ""
    changelog_text = ""
    repository_url = ""
    breaking_changes_detected = False
    deprecations_detected = False
    changelog_fetch_error = ""


class TestRunFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for TestRun model instances."""

    # Keep pytest from collecting this as a test class (name starts with
    # "Test"; factory_boy's __new__ triggers a PytestCollectionWarning).
    __test__ = False

    class Meta:
        model = TestRun

    pull_request = factory.SubFactory(PullRequestFactory)
    run_type = "pr_branch"
    status = "pending"
    test_scope = factory.LazyFunction(list)
    container_image = "python:3.12-slim"


class AgentFeedbackFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for AgentFeedback model instances."""

    class Meta:
        model = AgentFeedback

    pull_request = factory.SubFactory(PullRequestFactory)
    assessment = "good"
    feedback_body = factory.Faker("paragraph")
    feedback_method = "session-url"


class SecurityReportFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for SecurityReport model instances."""

    class Meta:
        model = SecurityReport

    project = factory.SubFactory(ProjectFactory)
    title = factory.Sequence(lambda n: f"Security Report #{n}")
    raw_text = factory.Faker("paragraph")
    source = "paste"
    reporter_name = ""
    reporter_email = ""
    status = "new"
    parsed_component = ""
    parsed_poc = ""
    parsed_impact = ""
    assessed_severity = "unknown"
    triage_summary = ""
    is_expected_behavior = False
    expected_behavior_explanation = ""
    poc_assessment = ""
    poc_plausible = None
    cve_matches = factory.LazyFunction(list)
    matched_cve_id = ""
    operator_notes = ""


class LLMBackendFallbackFactory(factory.django.DjangoModelFactory):  # type: ignore[misc]
    """Factory for LLMBackendFallback model instances."""

    class Meta:
        model = LLMBackendFallback

    provider = "openai"
    model = factory.Sequence(lambda n: f"gpt-4o-{n}")
    base_url = ""
    token_param = "max_completion_tokens"
    supports_json_object = True
