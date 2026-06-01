"""Tests for the review drafter pipeline orchestrator."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from franktheunicorn.config.models import (
    FineTunedModelConfig,
    LLMBackendConfig,
    OperatorConfig,
    ProjectConfig,
)
from franktheunicorn.core.models import ReviewDraft
from franktheunicorn.review.backends.base import PRContext, ReviewFinding
from franktheunicorn.review.drafter import (
    _coerce_severity,
    _maybe_inject_fine_tuned_model,
    build_pr_context,
    create_drafts_from_findings,
    draft_review,
)
from tests.factories import AntiPatternFactory


@pytest.mark.django_db
class TestBuildPRContext:
    """Tests for build_pr_context."""

    def test_basic_context_built(
        self,
        db_pr,
        spark_project_config,
        operator_config,
    ) -> None:
        ctx = build_pr_context(db_pr, spark_project_config, operator_config)
        assert isinstance(ctx, PRContext)
        assert ctx.pr_title == db_pr.title
        assert ctx.pr_number == db_pr.number
        assert ctx.pr_author == db_pr.author
        assert ctx.project_name == db_pr.project.full_name
        assert ctx.review_context == "ASF governance"
        assert ctx.review_style == "direct but kind"
        assert ctx.tone == "constructive"
        assert ctx.test_expectations == "tests required"

    def test_loads_anti_patterns(
        self,
        db_pr,
        spark_project_config,
        operator_config,
    ) -> None:
        # Create project-specific and global anti-patterns.
        AntiPatternFactory(pattern_text="don't nitpick imports", project=db_pr.project)
        AntiPatternFactory(pattern_text="avoid style nits", project=None)

        ctx = build_pr_context(db_pr, spark_project_config, operator_config)
        assert "don't nitpick imports" in ctx.anti_patterns
        assert "avoid style nits" in ctx.anti_patterns

    def test_personality_fields_populated(
        self,
        db_pr,
        spark_project_config,
    ) -> None:
        oc = OperatorConfig(
            github_username="holdenk",
            review_style="direct",
            personality="frank",
        )
        with patch("franktheunicorn.personalities.load_personality") as mock_load:
            mock_personality = type(
                "Personality",
                (),
                {
                    "identity": "I am Frank",
                    "internal_voice": "Think carefully",
                    "external_voice": "Be kind",
                    "review_philosophy": "Teach, don't lecture",
                },
            )()
            mock_load.return_value = mock_personality
            ctx = build_pr_context(db_pr, spark_project_config, oc)

        assert ctx.personality_identity == "I am Frank"
        assert ctx.personality_internal_voice == "Think carefully"
        assert ctx.personality_external_voice == "Be kind"
        assert ctx.personality_review_philosophy == "Teach, don't lecture"

    def test_context_strings_passed_through(
        self,
        db_pr,
        spark_project_config,
        operator_config,
    ) -> None:
        ctx = build_pr_context(
            db_pr,
            spark_project_config,
            operator_config,
            community_context="mailing list discussion about X",
            jira_context="SPARK-1234: Fix scheduler",
            sentry_context="3 errors in scheduler.py",
        )
        assert ctx.community_context == "mailing list discussion about X"
        assert ctx.jira_context == "SPARK-1234: Fix scheduler"
        assert ctx.sentry_context == "3 errors in scheduler.py"


@pytest.mark.django_db
class TestDraftReview:
    """Tests for draft_review with stub backend."""

    def test_creates_review_drafts_with_stub_backend(
        self,
        db_pr,
        spark_project_config,
        operator_config,
    ) -> None:
        # The stub backend produces deterministic findings.
        drafts = draft_review(db_pr, spark_project_config, operator_config=operator_config)
        assert isinstance(drafts, list)
        # Stub backend should produce at least one draft.
        assert len(drafts) > 0
        for d in drafts:
            assert isinstance(d, ReviewDraft)
            assert d.pull_request_id == db_pr.pk
            assert d.status == "pending"
            assert isinstance(d.sources, list)
            assert len(d.sources) > 0

    def test_default_operator_config_when_none(
        self,
        db_pr,
        spark_project_config,
    ) -> None:
        # Should not raise when operator_config is None.
        drafts = draft_review(db_pr, spark_project_config, operator_config=None)
        assert isinstance(drafts, list)

    def test_passes_context_to_build_pr_context(
        self,
        db_pr,
        spark_project_config,
        operator_config,
    ) -> None:
        with patch(
            "franktheunicorn.review.drafter.build_pr_context",
            wraps=build_pr_context,
        ) as mock_build:
            draft_review(
                db_pr,
                spark_project_config,
                operator_config=operator_config,
                community_context="comm ctx",
                jira_context="jira ctx",
                sentry_context="sentry ctx",
            )
            mock_build.assert_called_once()
            _, kwargs = mock_build.call_args
            assert kwargs.get("community_context") == "comm ctx"
            assert kwargs.get("jira_context") == "jira ctx"
            assert kwargs.get("sentry_context") == "sentry ctx"


@pytest.mark.django_db
class TestCreateDraftsFromFindings:
    """Tests for create_drafts_from_findings."""

    def _make_finding(self, **overrides) -> ReviewFinding:
        defaults = {
            "file_path": "src/main.py",
            "line_number": 10,
            "title": "correctness: possible null deref",
            "body": "This variable may be None here.",
            "suggestion": "Add a None check.",
            "confidence": 0.8,
            "severity": "important",
        }
        defaults.update(overrides)
        return ReviewFinding(**defaults)

    def test_creates_drafts(self, db_pr) -> None:
        findings = [self._make_finding()]
        drafts = create_drafts_from_findings(
            db_pr,
            findings,
            source="agent",
            project=db_pr.project,
        )
        assert len(drafts) == 1
        d = drafts[0]
        assert d.pull_request_id == db_pr.pk
        assert d.file_path == "src/main.py"
        assert d.line_number == 10
        assert d.comment_body == "This variable may be None here."
        assert d.suggestion == "Add a None check."
        assert d.confidence == 0.8
        assert d.severity == "important"
        assert d.status == "pending"
        assert d.sources == ["agent"]

    def test_category_detection_from_title(self, db_pr) -> None:
        cases = [
            ("correctness: null check", "correctness"),
            ("style: import order", "style"),
            ("security: sql injection", "security"),
            ("test-coverage: missing test", "test-coverage"),
            ("architectural: coupling", "architectural"),
            ("naming: unclear variable", "naming"),
            ("suggested-change: rename", "suggested-change"),
            ("moderation: tone issue", "moderation"),
            ("something else entirely", "other"),
        ]
        for title, expected_category in cases:
            finding = self._make_finding(title=title)
            drafts = create_drafts_from_findings(
                db_pr,
                [finding],
                source="agent",
                project=db_pr.project,
            )
            assert drafts[0].category == expected_category, (
                f"Expected category '{expected_category}' for title '{title}', "
                f"got '{drafts[0].category}'"
            )

    def test_anti_pattern_gating_suppresses_finding(self, db_pr) -> None:
        AntiPatternFactory(
            pattern_text="possible null deref",
            project=db_pr.project,
        )
        findings = [self._make_finding(body="This has a possible null deref risk.")]
        drafts = create_drafts_from_findings(
            db_pr,
            [findings[0]],
            source="agent",
            project=db_pr.project,
        )
        assert len(drafts) == 0

    def test_anti_pattern_does_not_suppress_non_matching(self, db_pr) -> None:
        AntiPatternFactory(
            pattern_text="unrelated pattern xyz",
            project=db_pr.project,
        )
        findings = [self._make_finding()]
        drafts = create_drafts_from_findings(
            db_pr,
            [findings[0]],
            source="agent",
            project=db_pr.project,
        )
        assert len(drafts) == 1

    def test_code_context_extraction_with_diff(self, db_pr) -> None:
        diff = (
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -8,6 +8,7 @@\n"
            " existing line\n"
            " existing line\n"
            "+new line at 10\n"
            " existing line\n"
        )
        findings = [self._make_finding(file_path="src/main.py", line_number=10)]
        drafts = create_drafts_from_findings(
            db_pr,
            findings,
            source="agent",
            project=db_pr.project,
            diff=diff,
        )
        assert len(drafts) == 1
        # code_context should be populated (the exact content depends on unidiff parsing)
        # At minimum, it should not raise and should return a string.
        assert isinstance(drafts[0].code_context, str)

    def test_sources_is_list(self, db_pr) -> None:
        findings = [self._make_finding()]
        drafts = create_drafts_from_findings(
            db_pr,
            findings,
            source="claude",
            project=db_pr.project,
        )
        assert drafts[0].sources == ["claude"]

    def test_invalid_severity_defaults_to_nit(self, db_pr) -> None:
        findings = [self._make_finding(severity="banana")]
        drafts = create_drafts_from_findings(
            db_pr,
            findings,
            source="agent",
            project=db_pr.project,
        )
        assert drafts[0].severity == "nit"

    def test_tone_guard_flag_passed_through(self, db_pr) -> None:
        findings = [self._make_finding()]
        drafts = create_drafts_from_findings(
            db_pr,
            findings,
            source="agent",
            project=db_pr.project,
            tone_guard_applied=True,
        )
        assert drafts[0].tone_guard_applied is True


class TestMaybeInjectFineTunedModel:
    """Tests for _maybe_inject_fine_tuned_model."""

    def test_no_injection_when_disabled(self) -> None:
        backends = [LLMBackendConfig(provider="stub")]
        pc = ProjectConfig(
            owner="test",
            repo="repo",
            fine_tuned_model=FineTunedModelConfig(enabled=False),
        )
        result = _maybe_inject_fine_tuned_model(backends, pc)
        assert len(result) == 1
        assert result[0].provider == "stub"

    def test_no_injection_when_no_model(self) -> None:
        backends = [LLMBackendConfig(provider="stub")]
        pc = ProjectConfig(
            owner="test",
            repo="repo",
            fine_tuned_model=FineTunedModelConfig(enabled=True, model=""),
        )
        result = _maybe_inject_fine_tuned_model(backends, pc)
        assert len(result) == 1

    def test_injection_when_enabled(self) -> None:
        backends = [LLMBackendConfig(provider="stub")]
        pc = ProjectConfig(
            owner="test",
            repo="repo",
            fine_tuned_model=FineTunedModelConfig(
                enabled=True,
                provider="ollama",
                model="my-fine-tuned:latest",
                endpoint="http://localhost:11434",
            ),
        )
        result = _maybe_inject_fine_tuned_model(backends, pc)
        assert len(result) == 2
        # Fine-tuned model should be first (first-pass slot).
        assert result[0].provider == "ollama"
        assert result[0].model == "my-fine-tuned:latest"
        assert result[0].base_url == "http://localhost:11434"
        # Original backend should follow.
        assert result[1].provider == "stub"


class TestCoerceSeverity:
    """LLM-returned severities must map to ReviewDraft.SEVERITY_CHOICES."""

    def test_canonical_values_pass_through(self) -> None:
        assert _coerce_severity("critical") == "critical"
        assert _coerce_severity("important") == "important"
        assert _coerce_severity("nit") == "nit"
        assert _coerce_severity("informational") == "informational"

    def test_high_maps_to_important(self) -> None:
        # The default ReviewFinding.severity was "medium" and prompts
        # historically allowed "high". Without coercion these silently
        # downgraded to "nit". "high" must surface as "important".
        assert _coerce_severity("high") == "important"

    def test_medium_low_map_to_nit(self) -> None:
        assert _coerce_severity("medium") == "nit"
        assert _coerce_severity("low") == "nit"

    def test_info_aliases(self) -> None:
        assert _coerce_severity("info") == "informational"
        assert _coerce_severity("trivial") == "informational"

    def test_case_insensitive(self) -> None:
        assert _coerce_severity("HIGH") == "important"
        assert _coerce_severity("Critical") == "critical"

    def test_unknown_warns_and_falls_back_to_nit(self, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="franktheunicorn.review.drafter"):
            assert _coerce_severity("blocker", "title here") == "nit"
        assert any("blocker" in rec.message for rec in caplog.records)

    def test_empty_falls_back_to_nit(self) -> None:
        assert _coerce_severity("") == "nit"


@pytest.mark.django_db
class TestSeverityCoercionInDrafts:
    """End-to-end: a finding with severity='high' must persist as 'important'."""

    def test_high_severity_finding_persists_as_important(self, db_pr) -> None:
        finding = ReviewFinding(
            file_path="src/main.py",
            line_number=10,
            title="correctness: null deref",
            body="Will crash at runtime.",
            confidence=0.9,
            severity="high",
        )
        drafts = create_drafts_from_findings(
            db_pr, [finding], source="agent", project=db_pr.project
        )
        assert len(drafts) == 1
        assert drafts[0].severity == "important"

    def test_default_medium_severity_persists_as_nit(self, db_pr) -> None:
        # ReviewFinding's default severity is "medium" — must not crash
        # and must coerce to a valid model choice.
        finding = ReviewFinding(
            file_path="src/main.py",
            line_number=10,
            title="style: minor nit",
            body="Use snake_case.",
        )
        assert finding.severity == "medium"
        drafts = create_drafts_from_findings(
            db_pr, [finding], source="agent", project=db_pr.project
        )
        assert len(drafts) == 1
        assert drafts[0].severity == "nit"


@pytest.mark.django_db
class TestSourcesPerFinding:
    """Per-finding source attribution survives dedup and persists on the draft."""

    def test_single_source_falls_back_to_source_param(self, db_pr) -> None:
        finding = ReviewFinding(
            file_path="src/main.py",
            line_number=10,
            title="correctness: x",
            body="body",
            severity="important",
        )
        drafts = create_drafts_from_findings(
            db_pr, [finding], source="agent", project=db_pr.project
        )
        assert drafts[0].sources == ["agent"]
        assert drafts[0].backend_used == "agent"

    def test_sources_per_finding_overrides_source(self, db_pr) -> None:
        finding = ReviewFinding(
            file_path="src/main.py",
            line_number=10,
            title="correctness: x",
            body="body",
            severity="important",
        )
        drafts = create_drafts_from_findings(
            db_pr,
            [finding],
            source="agent",
            project=db_pr.project,
            sources_per_finding=["claude,coderabbit"],
        )
        assert drafts[0].sources == ["claude", "coderabbit"]
        # backend_used reflects the primary (first) source.
        assert drafts[0].backend_used == "claude"

    def test_empty_sources_entry_falls_back_to_source(self, db_pr) -> None:
        finding = ReviewFinding(
            file_path="src/main.py",
            line_number=10,
            title="correctness: x",
            body="body",
            severity="important",
        )
        drafts = create_drafts_from_findings(
            db_pr,
            [finding],
            source="agent",
            project=db_pr.project,
            sources_per_finding=[""],
        )
        assert drafts[0].sources == ["agent"]


@pytest.mark.django_db
class TestDraftReviewMultiBackendAttribution:
    """End-to-end regression for the source-attribution bug.

    Before the fix, draft_review() pinned every persisted draft to the source
    of whichever backend ran first. This test patches the single-backend
    runner so two backends contribute findings on different files and asserts
    that both backend names appear across the resulting drafts.
    """

    def test_two_backends_contribute_distinct_sources(
        self,
        db_pr,
        spark_project_config,
    ) -> None:
        from franktheunicorn.config.models import LLMBackendConfig
        from franktheunicorn.review.backends.base import ReviewResult

        operator_config = OperatorConfig(
            llm_backends=[
                LLMBackendConfig(provider="stub"),
                LLMBackendConfig(provider="stub"),
            ],
        )

        # Distinct findings on different files so dedup can't merge them,
        # and distinct source names so we can verify per-finding attribution.
        finding_a = ReviewFinding(
            file_path="src/a.py",
            line_number=1,
            title="correctness: a",
            body="finding from claude",
            severity="important",
        )
        finding_b = ReviewFinding(
            file_path="src/b.py",
            line_number=1,
            title="correctness: b",
            body="finding from coderabbit",
            severity="important",
        )

        call_results = [
            ("claude", ReviewResult(overall_vibe="", findings=[finding_a]), False),
            ("coderabbit", ReviewResult(overall_vibe="", findings=[finding_b]), False),
        ]

        with patch(
            "franktheunicorn.review.drafter._run_single_backend",
            side_effect=call_results,
        ):
            drafts = draft_review(db_pr, spark_project_config, operator_config)

        sources_seen: set[str] = set()
        for d in drafts:
            sources_seen.update(d.sources)
        assert "claude" in sources_seen
        assert "coderabbit" in sources_seen
