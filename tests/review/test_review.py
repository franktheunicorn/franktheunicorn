"""Tests for the review drafter and anti-pattern system."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from franktheunicorn.config.models import OperatorConfig, ProjectConfig
from franktheunicorn.core.models import AntiPattern, Project, PullRequest, ReviewDraft
from franktheunicorn.review.antipattern import check_against_anti_patterns, record_anti_pattern
from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.drafter import (
    _extract_code_context,
    _maybe_inject_fine_tuned_model,
    create_drafts_from_findings,
    draft_review,
)


@pytest.mark.django_db
class TestDraftReview:
    def test_generates_drafts(
        self,
        db_pr: PullRequest,
        spark_project_config: ProjectConfig,
        operator_config: OperatorConfig,
    ) -> None:
        drafts = draft_review(db_pr, spark_project_config, operator_config)
        assert len(drafts) > 0
        assert all(isinstance(d, ReviewDraft) for d in drafts)

    def test_drafts_are_deterministic(
        self,
        db_pr: PullRequest,
        spark_project_config: ProjectConfig,
        operator_config: OperatorConfig,
    ) -> None:
        """Same input should produce same output."""
        drafts1 = draft_review(db_pr, spark_project_config, operator_config)
        # Clear drafts and regenerate
        ReviewDraft.objects.filter(pull_request=db_pr).delete()
        drafts2 = draft_review(db_pr, spark_project_config, operator_config)
        assert len(drafts1) == len(drafts2)
        assert drafts1[0].comment_body == drafts2[0].comment_body
        assert drafts1[0].file_path == drafts2[0].file_path

    def test_draft_fields(
        self,
        db_pr: PullRequest,
        spark_project_config: ProjectConfig,
        operator_config: OperatorConfig,
    ) -> None:
        drafts = draft_review(db_pr, spark_project_config, operator_config)
        for d in drafts:
            assert d.file_path != ""
            assert d.line_number is not None and d.line_number > 0
            assert d.comment_body != ""
            assert d.status == "pending"
            assert 0.0 <= d.confidence <= 1.0
            assert d.sources == ["agent"]

    def test_backwards_compatible_without_operator_config(
        self,
        db_pr: PullRequest,
        spark_project_config: ProjectConfig,
    ) -> None:
        """draft_review should work without operator_config for backwards compatibility."""
        drafts = draft_review(db_pr, spark_project_config)
        assert len(drafts) > 0
        assert all(d.sources == ["agent"] for d in drafts)

    def test_multiple_backends_combine_findings(
        self,
        db_pr: PullRequest,
        spark_project_config: ProjectConfig,
    ) -> None:
        """Multiple backends should each contribute findings."""
        from franktheunicorn.config.models import LLMBackendConfig

        # Two stub backends — each produces findings independently.
        multi_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(), LLMBackendConfig()],
        )
        drafts = draft_review(db_pr, spark_project_config, multi_config)
        # Two stub backends, each producing up to 2 findings from 2 changed_files.
        assert len(drafts) >= 2

    def test_legacy_single_llm_config_still_works(
        self,
        db_pr: PullRequest,
        spark_project_config: ProjectConfig,
    ) -> None:
        """Legacy ``llm:`` field is promoted to ``llm_backends``."""
        from franktheunicorn.config.models import LLMBackendConfig

        legacy_config = OperatorConfig(llm=LLMBackendConfig(provider="stub"))
        assert len(legacy_config.llm_backends) == 1
        drafts = draft_review(db_pr, spark_project_config, legacy_config)
        assert len(drafts) > 0


@pytest.mark.django_db
class TestAntiPattern:
    def test_record_new_pattern(self, db_project: Project) -> None:
        ap = record_anti_pattern(
            "nit: ",
            description="Avoid nitpicky comments",
            project=db_project,
        )
        assert ap.pattern_text == "nit: "
        assert ap.times_triggered == 0

    def test_record_existing_increments(self, db_project: Project) -> None:
        record_anti_pattern("nit: ", project=db_project)
        ap = record_anti_pattern("nit: ", project=db_project)
        assert ap.times_triggered == 1

    def test_check_matches(self, db_project: Project) -> None:
        AntiPattern.objects.create(
            pattern_text="nit:",
            project=db_project,
        )
        matches = check_against_anti_patterns("nit: fix the spacing here", db_project)
        assert len(matches) == 1

    def test_check_no_match(self, db_project: Project) -> None:
        AntiPattern.objects.create(
            pattern_text="nit:",
            project=db_project,
        )
        matches = check_against_anti_patterns("Great improvement!", db_project)
        assert len(matches) == 0


class TestFineTunedModelInjection:
    def test_no_injection_when_disabled(self) -> None:
        from franktheunicorn.config.models import LLMBackendConfig

        backends = [LLMBackendConfig(provider="stub")]
        config = ProjectConfig(owner="x", repo="y")

        result = _maybe_inject_fine_tuned_model(backends, config)
        assert len(result) == 1
        assert result[0].provider == "stub"

    def test_injects_when_enabled(self) -> None:
        from franktheunicorn.config.models import FineTunedModelConfig, LLMBackendConfig

        backends = [LLMBackendConfig(provider="claude")]
        config = ProjectConfig(
            owner="x",
            repo="y",
            fine_tuned_model=FineTunedModelConfig(
                enabled=True,
                provider="ollama",
                model="franktheunicorn-spark-v1",
                endpoint="http://localhost:11434",
            ),
        )

        result = _maybe_inject_fine_tuned_model(backends, config)
        assert len(result) == 2
        assert result[0].provider == "ollama"
        assert result[0].model == "franktheunicorn-spark-v1"
        assert result[1].provider == "claude"

    def test_no_injection_without_model_name(self) -> None:
        from franktheunicorn.config.models import FineTunedModelConfig, LLMBackendConfig

        backends = [LLMBackendConfig(provider="stub")]
        config = ProjectConfig(
            owner="x",
            repo="y",
            fine_tuned_model=FineTunedModelConfig(enabled=True, model=""),
        )

        result = _maybe_inject_fine_tuned_model(backends, config)
        assert len(result) == 1  # not injected


class TestExtractCodeContext:
    def test_extracts_hunk(self) -> None:
        diff = (
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            "+import sys\n"
            " \n"
            " def main():\n"
        )
        result = _extract_code_context(diff, "src/main.py", 2)
        assert "import sys" in result

    def test_empty_diff(self) -> None:
        assert _extract_code_context("", "src/main.py", 1) == ""

    def test_empty_file_path(self) -> None:
        assert _extract_code_context("some diff", "", 1) == ""

    def test_file_not_in_diff(self) -> None:
        diff = "--- a/other.py\n+++ b/other.py\n@@ -1,2 +1,2 @@\n-old\n+new\n"
        assert _extract_code_context(diff, "src/main.py", 1) == ""

    def test_no_line_number_returns_first_hunk(self) -> None:
        diff = "--- a/src/main.py\n+++ b/src/main.py\n@@ -1,2 +1,3 @@\n import os\n+import sys\n \n"
        result = _extract_code_context(diff, "src/main.py", None)
        assert result != ""


@pytest.mark.django_db
class TestRejectionPredictorIntegration:
    def test_no_model_no_rejection_probability(self, db_pr: PullRequest) -> None:
        """Without a trained model, rejection_probability should be None."""
        findings = [
            ReviewFinding(
                file_path="src/main.py",
                line_number=10,
                title="correctness: fix bug",
                body="This will fail at runtime.",
                confidence=0.8,
                severity="important",
            )
        ]
        drafts = create_drafts_from_findings(db_pr, findings, source="agent", project=db_pr.project)
        assert len(drafts) == 1
        assert drafts[0].rejection_probability is None
        assert drafts[0].is_auto_suppressed is False

    def test_with_model_sets_rejection_probability(self, db_pr: PullRequest) -> None:
        """With a trained model, rejection_probability should be set."""
        from franktheunicorn.scoring.rejection_predictor import RejectionPredictor

        mock_predictor = RejectionPredictor()
        mock_predictor._trained = True
        # Train with minimal data so predict_proba works.
        mock_predictor.model.fit([{"category": "style"}, {"category": "correctness"}], [1, 0])

        findings = [
            ReviewFinding(
                file_path="src/main.py",
                line_number=10,
                title="style: fix naming",
                body="Use snake_case.",
                confidence=0.5,
                severity="nit",
            )
        ]
        with patch(
            "franktheunicorn.review.drafter.load_predictor_for_project",
            return_value=mock_predictor,
        ):
            drafts = create_drafts_from_findings(
                db_pr, findings, source="agent", project=db_pr.project
            )
        assert len(drafts) == 1
        assert drafts[0].rejection_probability is not None
        assert 0.0 <= drafts[0].rejection_probability <= 1.0

    def test_auto_suppress_high_probability(self, db_pr: PullRequest) -> None:
        """Findings with P(rejection) > 0.8 should be auto-suppressed."""
        from franktheunicorn.scoring.rejection_predictor import RejectionPredictor

        mock_predictor = RejectionPredictor()
        mock_predictor._trained = True

        findings = [
            ReviewFinding(
                file_path="src/main.py",
                line_number=10,
                title="style: nit",
                body="Use snake_case.",
                confidence=0.5,
                severity="nit",
            )
        ]
        with (
            patch(
                "franktheunicorn.review.drafter.load_predictor_for_project",
                return_value=mock_predictor,
            ),
            patch.object(mock_predictor, "predict_rejection", return_value=0.9),
        ):
            drafts = create_drafts_from_findings(
                db_pr, findings, source="agent", project=db_pr.project
            )
        assert len(drafts) == 1
        assert drafts[0].is_auto_suppressed is True
        assert drafts[0].rejection_probability == 0.9

    def test_not_suppressed_below_threshold(self, db_pr: PullRequest) -> None:
        """Findings with P(rejection) <= 0.8 should not be suppressed."""
        from franktheunicorn.scoring.rejection_predictor import RejectionPredictor

        mock_predictor = RejectionPredictor()
        mock_predictor._trained = True

        findings = [
            ReviewFinding(
                file_path="src/main.py",
                line_number=10,
                title="correctness: bug",
                body="This will crash.",
                confidence=0.9,
                severity="critical",
            )
        ]
        with (
            patch(
                "franktheunicorn.review.drafter.load_predictor_for_project",
                return_value=mock_predictor,
            ),
            patch.object(mock_predictor, "predict_rejection", return_value=0.3),
        ):
            drafts = create_drafts_from_findings(
                db_pr, findings, source="agent", project=db_pr.project
            )
        assert len(drafts) == 1
        assert drafts[0].is_auto_suppressed is False
        assert drafts[0].rejection_probability == 0.3

    def test_code_context_populated_from_diff(self, db_pr: PullRequest) -> None:
        """Code context should be extracted from the diff."""
        diff = (
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -8,3 +8,4 @@\n"
            " import os\n"
            "+import sys\n"
            " \n"
            " def main():\n"
        )
        findings = [
            ReviewFinding(
                file_path="src/main.py",
                line_number=9,
                title="style: unnecessary import",
                body="Remove unused import.",
                confidence=0.6,
                severity="nit",
            )
        ]
        drafts = create_drafts_from_findings(
            db_pr, findings, source="agent", project=db_pr.project, diff=diff
        )
        assert len(drafts) == 1
        assert "import sys" in drafts[0].code_context

    def test_retrain_called_after_persist(self, db_pr: PullRequest) -> None:
        """maybe_retrain should be called after persisting drafts."""
        findings = [
            ReviewFinding(
                file_path="src/main.py",
                line_number=10,
                title="correctness: fix",
                body="Fix this.",
                confidence=0.8,
                severity="important",
            )
        ]
        with patch("franktheunicorn.review.drafter.maybe_retrain") as mock_retrain:
            create_drafts_from_findings(db_pr, findings, source="agent", project=db_pr.project)
            mock_retrain.assert_called_once_with(
                db_pr.project.pk, db_pr.project.owner, db_pr.project.repo
            )
