"""Tests for the review drafter pipeline orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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


@pytest.mark.django_db
class TestFetchLinkedIssuesContext:
    """Tests for fetch_linked_issues_context."""

    def test_no_hash_in_text_returns_empty(self, db_pr) -> None:
        from franktheunicorn.review.drafter import fetch_linked_issues_context

        db_pr.title = "Fix race condition"
        db_pr.body = "No issue reference here"
        db_pr.save()

        result = fetch_linked_issues_context(db_pr)
        assert result == ""

    def test_hash_with_issues_returns_formatted_context(self, db_pr) -> None:
        from franktheunicorn.review.drafter import fetch_linked_issues_context

        db_pr.title = "Fix race condition"
        db_pr.body = "Fixes #1234"
        db_pr.save()

        mock_issue = type(
            "Issue",
            (),
            {"to_prompt_context": lambda self: "Issue #1234: Fix scheduler race condition"},
        )()

        # The import is inside the function, so patch the class at its definition site
        with patch(
            "franktheunicorn.data_access.github.issue_fetcher.IssueFetcher"
        ) as mock_fetcher_cls:
            mock_fetcher_cls.return_value.fetch_linked_issues.return_value = [mock_issue]
            result = fetch_linked_issues_context(db_pr)

        assert "Issue #1234" in result

    def test_hash_with_no_issues_returns_empty(self, db_pr) -> None:
        from franktheunicorn.review.drafter import fetch_linked_issues_context

        db_pr.title = "Fix #42 race condition"
        db_pr.body = ""
        db_pr.save()

        with patch(
            "franktheunicorn.data_access.github.issue_fetcher.IssueFetcher"
        ) as mock_fetcher_cls:
            mock_fetcher_cls.return_value.fetch_linked_issues.return_value = []
            result = fetch_linked_issues_context(db_pr)

        assert result == ""

    def test_exception_returns_empty(self, db_pr) -> None:
        from franktheunicorn.review.drafter import fetch_linked_issues_context

        db_pr.title = "Fix #42"
        db_pr.body = ""
        db_pr.save()

        with patch(
            "franktheunicorn.data_access.github.issue_fetcher.IssueFetcher",
            side_effect=RuntimeError("API error"),
        ):
            result = fetch_linked_issues_context(db_pr)

        assert result == ""


@pytest.mark.django_db
class TestBuildPRContextEdgeCases:
    """Edge cases for build_pr_context exception handling."""

    def test_anti_patterns_exception_is_swallowed(
        self, db_pr, spark_project_config, operator_config
    ) -> None:
        """An exception loading anti-patterns should not crash build_pr_context."""
        with patch(
            "franktheunicorn.core.models.AntiPattern.objects.filter",
            side_effect=RuntimeError("DB error"),
        ):
            ctx = build_pr_context(db_pr, spark_project_config, operator_config)
        assert ctx.anti_patterns == []

    def test_context_strings_exception_is_swallowed(
        self, db_pr, spark_project_config, operator_config
    ) -> None:
        """An exception building context strings should not crash build_pr_context."""
        with patch(
            "franktheunicorn.review.drafter.build_context_strings",
            side_effect=RuntimeError("IO error"),
        ):
            ctx = build_pr_context(db_pr, spark_project_config, operator_config)
        assert ctx.full_file_context == ""
        assert ctx.imported_modules_context == ""


class TestGetPrDiff:
    """Tests for _get_pr_diff."""

    def test_returns_provided_diff(self) -> None:
        from unittest.mock import MagicMock

        from franktheunicorn.review.drafter import _get_pr_diff

        pr = MagicMock()
        result = _get_pr_diff(pr, diff="--- a/file.py\n+++ b/file.py\n")
        assert result == "--- a/file.py\n+++ b/file.py\n"

    def test_placeholder_from_changed_files(self) -> None:
        from unittest.mock import MagicMock

        from franktheunicorn.review.drafter import _get_pr_diff

        pr = MagicMock()
        pr.changed_files = ["src/main.py", "src/utils.py"]
        result = _get_pr_diff(pr, diff="")
        assert "+++ b/src/main.py" in result
        assert "+++ b/src/utils.py" in result

    def test_fallback_when_no_changed_files(self) -> None:
        from unittest.mock import MagicMock

        from franktheunicorn.review.drafter import _get_pr_diff

        pr = MagicMock()
        pr.changed_files = []
        result = _get_pr_diff(pr, diff="")
        assert result == "+++ b/unknown_file.py\n"


class TestExtractCodeContext:
    """Tests for _extract_code_context."""

    def test_empty_diff_returns_empty(self) -> None:
        from franktheunicorn.review.drafter import _extract_code_context

        result = _extract_code_context("", "src/main.py", 10)
        assert result == ""

    def test_empty_file_path_returns_empty(self) -> None:
        from franktheunicorn.review.drafter import _extract_code_context

        result = _extract_code_context("--- a/file.py\n+++ b/file.py\n", "", 10)
        assert result == ""

    def test_none_line_number_returns_first_hunk(self) -> None:
        from franktheunicorn.review.drafter import _extract_code_context

        diff = (
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            "+import sys\n"
            " def main():\n"
            "     pass\n"
        )
        result = _extract_code_context(diff, "src/main.py", None)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_invalid_diff_returns_empty(self) -> None:
        from franktheunicorn.review.drafter import _extract_code_context

        result = _extract_code_context("this is not a diff", "src/main.py", 10)
        assert result == ""

    def test_no_hunk_match_returns_first_hunk(self) -> None:
        """When line_number doesn't match any hunk, return the first hunk."""
        from franktheunicorn.review.drafter import _extract_code_context

        diff = (
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            "+import sys\n"
            " def main():\n"
            "     pass\n"
        )
        # Line 999 is beyond the hunk range, so falls back to first hunk
        result = _extract_code_context(diff, "src/main.py", 999)
        assert isinstance(result, str)
        assert len(result) > 0


@pytest.mark.django_db
class TestRunSingleBackend:
    """Tests for _run_single_backend."""

    def test_uses_generate_review_when_available(self, db_pr) -> None:
        from franktheunicorn.config.models import LLMBackendConfig
        from franktheunicorn.review.backends.base import ReviewFinding, ReviewResult
        from franktheunicorn.review.drafter import _run_single_backend
        from tests.conftest import make_pr_context

        backend_config = LLMBackendConfig(provider="stub")
        pr_context = make_pr_context()
        expected_result = ReviewResult(
            overall_vibe="Looks good overall",
            findings=[ReviewFinding(file_path="a.py", title="t", body="b", confidence=0.8)],
        )

        with patch(
            "franktheunicorn.review.backends.stub_backend.StubBackend.generate_review",
            return_value=expected_result,
        ):
            source, result = _run_single_backend(backend_config, "diff", pr_context)

        assert source == "agent"  # stub maps to "agent"
        assert result.overall_vibe == "Looks good overall"
        assert len(result.findings) == 1

    def test_backend_exception_returns_empty_result(self, db_pr) -> None:
        from franktheunicorn.config.models import LLMBackendConfig
        from franktheunicorn.review.backends.base import ReviewResult
        from franktheunicorn.review.drafter import _run_single_backend
        from tests.conftest import make_pr_context

        backend_config = LLMBackendConfig(provider="stub")
        pr_context = make_pr_context()

        with patch(
            "franktheunicorn.review.backends.stub_backend.StubBackend.generate_review",
            side_effect=RuntimeError("LLM crashed"),
        ):
            _source, result = _run_single_backend(backend_config, "diff", pr_context)

        assert isinstance(result, ReviewResult)
        assert result.findings == []

    def test_generate_findings_fallback_for_backend_without_generate_review(self) -> None:
        """Backend without generate_review falls back to generate_findings."""
        from franktheunicorn.config.models import LLMBackendConfig
        from franktheunicorn.review.backends.base import ReviewFinding
        from franktheunicorn.review.drafter import _run_single_backend
        from tests.conftest import make_pr_context

        backend_config = LLMBackendConfig(provider="stub")
        pr_context = make_pr_context()

        expected_findings = [ReviewFinding(file_path="b.py", title="t", body="b")]

        # Create a mock backend with generate_findings but no generate_review
        mock_backend = MagicMock(spec=["generate_findings"])
        mock_backend.generate_findings.return_value = expected_findings

        with patch("franktheunicorn.review.drafter.get_backend", return_value=mock_backend):
            source, result = _run_single_backend(backend_config, "diff", pr_context)

        # hasattr(mock_backend, "generate_review") is False → uses generate_findings
        assert source == "agent"  # provider="stub" maps to "agent"
        assert len(result.findings) == 1


@pytest.mark.django_db
class TestDraftReviewEdgeCases:
    """Edge cases for draft_review."""

    def test_returns_empty_when_all_backends_produce_no_findings(
        self, db_pr, spark_project_config, operator_config
    ) -> None:
        from franktheunicorn.review.backends.base import ReviewResult

        with patch(
            "franktheunicorn.review.drafter._run_single_backend",
            return_value=("agent", ReviewResult(findings=[])),
        ):
            from franktheunicorn.review.drafter import draft_review

            drafts = draft_review(db_pr, spark_project_config, operator_config=operator_config)

        assert drafts == []

    def test_vibe_is_persisted_when_backend_produces_one(
        self, db_pr, spark_project_config, operator_config
    ) -> None:
        from franktheunicorn.core.models import AgentVibe
        from franktheunicorn.review.backends.base import ReviewFinding, ReviewResult
        from franktheunicorn.review.drafter import draft_review

        result_with_vibe = ReviewResult(
            overall_vibe="Looks good, minor nits only",
            findings=[
                ReviewFinding(
                    file_path="src/main.py",
                    title="nit: trailing whitespace",
                    body="Remove trailing space.",
                    confidence=0.6,
                )
            ],
        )

        with patch(
            "franktheunicorn.review.drafter._run_single_backend",
            return_value=("agent", result_with_vibe),
        ):
            draft_review(db_pr, spark_project_config, operator_config=operator_config)

        vibes = AgentVibe.objects.filter(pull_request=db_pr)
        assert vibes.exists()
        assert vibes.first().vibe_text == "Looks good, minor nits only"

    def test_vibe_persistence_exception_does_not_crash(
        self, db_pr, spark_project_config, operator_config
    ) -> None:
        from franktheunicorn.review.backends.base import ReviewFinding, ReviewResult
        from franktheunicorn.review.drafter import draft_review

        result_with_vibe = ReviewResult(
            overall_vibe="Nice work",
            findings=[
                ReviewFinding(
                    file_path="src/main.py",
                    title="style: minor",
                    body="Nit.",
                    confidence=0.5,
                )
            ],
        )

        with (
            patch(
                "franktheunicorn.review.drafter._run_single_backend",
                return_value=("agent", result_with_vibe),
            ),
            patch(
                "franktheunicorn.core.models.AgentVibe.objects.update_or_create",
                side_effect=RuntimeError("DB error"),
            ),
        ):
            # Should not raise even though vibe persistence failed
            drafts = draft_review(db_pr, spark_project_config, operator_config=operator_config)

        assert isinstance(drafts, list)

    def test_maybe_retrain_exception_does_not_crash(self, db_pr) -> None:
        from franktheunicorn.review.backends.base import ReviewFinding
        from franktheunicorn.review.drafter import create_drafts_from_findings

        findings = [
            ReviewFinding(
                file_path="src/main.py",
                title="style: minor",
                body="Some nit.",
                confidence=0.5,
            )
        ]

        with patch(
            "franktheunicorn.review.drafter.maybe_retrain",
            side_effect=RuntimeError("retrain error"),
        ):
            # Should not raise
            drafts = create_drafts_from_findings(
                db_pr,
                findings,
                source="agent",
                project=db_pr.project,
            )

        assert len(drafts) == 1


@pytest.mark.django_db
class TestToneGuardApplication:
    """Tests for tone guard application in draft_review."""

    def test_tone_guard_applied_when_non_stub_tone_backend(self, db_pr) -> None:
        """When tone_backend.provider != 'stub', apply_tone_guard_batch is called."""
        from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, ProjectConfig
        from franktheunicorn.review.backends.base import ReviewFinding, ReviewResult
        from franktheunicorn.review.drafter import draft_review

        project_config = ProjectConfig(owner="apache", repo="spark")
        # A non-stub first backend means tone_backend.provider != "stub"
        op_config = OperatorConfig(
            github_username="holdenk",
            llm_backends=[LLMBackendConfig(provider="claude")],
        )

        result_with_findings = ReviewResult(
            findings=[
                ReviewFinding(
                    file_path="src/main.py",
                    title="style: minor",
                    body="Please change this.",
                    confidence=0.6,
                )
            ]
        )

        with (
            patch(
                "franktheunicorn.review.drafter._run_single_backend",
                return_value=("agent", result_with_findings),
            ),
            patch(
                "franktheunicorn.review.drafter.apply_tone_guard_batch",
                return_value=result_with_findings.findings,
            ) as mock_tone,
        ):
            draft_review(db_pr, project_config, operator_config=op_config)

        mock_tone.assert_called_once()


@pytest.mark.django_db
class TestGenerateFindingsFallback:
    """Explicit test for _run_single_backend else-branch (generate_findings)."""

    def test_else_branch_uses_generate_findings(self) -> None:
        """When backend has no generate_review, generate_findings is called."""
        from franktheunicorn.config.models import LLMBackendConfig
        from franktheunicorn.review.backends.base import ReviewFinding
        from franktheunicorn.review.drafter import _run_single_backend
        from tests.conftest import make_pr_context

        backend_config = LLMBackendConfig(provider="stub")
        pr_context = make_pr_context()

        expected_findings = [ReviewFinding(file_path="c.py", title="t", body="b")]

        # Build a mock backend spec that excludes generate_review
        mock_backend = MagicMock(spec=["generate_findings"])
        mock_backend.generate_findings.return_value = expected_findings

        # Must patch at the drafter module's binding, not the source module
        with patch("franktheunicorn.review.drafter.get_backend", return_value=mock_backend):
            _source, result = _run_single_backend(backend_config, "diff", pr_context)

        mock_backend.generate_findings.assert_called_once_with("diff", pr_context)
        assert len(result.findings) == 1
