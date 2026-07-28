"""Tests for the security triage iterative learning loop.

Mirrors the anti-pattern learning loop tests, but for triage feedback:
agree/disagree feedback is recorded, distilled into learned guidance by an
LLM, and resolved (project-over-global precedence) for future triage
prompts.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
from franktheunicorn.core.models import SecurityTriageFeedback, SecurityTriageGuidance
from franktheunicorn.security.learning import (
    distill_triage_guidance,
    record_triage_feedback,
    resolve_triage_guidance,
)
from tests.factories import (
    ProjectFactory,
    SecurityReportFactory,
    SecurityTriageFeedbackFactory,
    SecurityTriageGuidanceFactory,
)


@pytest.fixture
def operator_config_with_llm() -> OperatorConfig:
    return OperatorConfig(
        github_username="holdenk",
        llm_backends=[LLMBackendConfig(provider="stub")],
    )


@pytest.fixture
def operator_config_no_llm() -> OperatorConfig:
    return OperatorConfig(github_username="holdenk")


def _stub_backend(response: str) -> MagicMock:
    backend = MagicMock()
    backend.complete.return_value = response
    return backend


@pytest.mark.django_db
class TestRecordTriageFeedback:
    def test_creates_feedback_row(self, operator_config_no_llm: OperatorConfig) -> None:
        report = SecurityReportFactory(
            triage_summary="Looks like a real finding.",
            assessed_severity="high",
        )
        feedback = record_triage_feedback(report, True, "Good catch", operator_config_no_llm)

        assert feedback.pk is not None
        assert feedback.agreed is True
        assert feedback.operator_comment == "Good catch"
        assert feedback.triage_summary_snapshot == "Looks like a real finding."
        assert feedback.assessed_severity_snapshot == "high"
        assert feedback.report == report
        assert feedback.project == report.project
        assert SecurityTriageFeedback.objects.count() == 1

    def test_triggers_distillation(self, operator_config_with_llm: OperatorConfig) -> None:
        report = SecurityReportFactory(triage_summary="x", assessed_severity="low")
        with patch("franktheunicorn.review.backends.get_backend") as mock_get_backend:
            mock_get_backend.return_value = _stub_backend("- Treat X as expected behavior.")
            record_triage_feedback(report, False, "Not a real bug", operator_config_with_llm)

        guidance = SecurityTriageGuidance.objects.get(project=report.project)
        assert "Treat X as expected behavior." in guidance.guidance_text
        assert guidance.source_feedback_count == 1

    def test_distillation_failure_does_not_break_feedback_save(
        self, operator_config_with_llm: OperatorConfig
    ) -> None:
        """A distillation error must never prevent the feedback row from saving."""
        report = SecurityReportFactory()
        with patch(
            "franktheunicorn.security.learning.distill_triage_guidance",
            side_effect=RuntimeError("boom"),
        ):
            feedback = record_triage_feedback(report, True, "", operator_config_with_llm)

        assert feedback.pk is not None
        assert SecurityTriageFeedback.objects.filter(pk=feedback.pk).exists()


@pytest.mark.django_db
class TestDistillTriageGuidance:
    def test_no_feedback_returns_none(self, operator_config_with_llm: OperatorConfig) -> None:
        project = ProjectFactory()
        assert distill_triage_guidance(project, operator_config_with_llm) is None

    def test_no_llm_backend_returns_none(self, operator_config_no_llm: OperatorConfig) -> None:
        project = ProjectFactory()
        SecurityTriageFeedbackFactory(project=project)
        assert distill_triage_guidance(project, operator_config_no_llm) is None

    def test_empty_llm_response_returns_none(
        self, operator_config_with_llm: OperatorConfig
    ) -> None:
        project = ProjectFactory()
        SecurityTriageFeedbackFactory(project=project)
        with patch("franktheunicorn.review.backends.get_backend") as mock_get_backend:
            mock_get_backend.return_value = _stub_backend("")
            assert distill_triage_guidance(project, operator_config_with_llm) is None

    def test_upserts_active_guidance(self, operator_config_with_llm: OperatorConfig) -> None:
        project = ProjectFactory()
        SecurityTriageFeedbackFactory(project=project, agreed=True)
        SecurityTriageFeedbackFactory(project=project, agreed=False)

        with patch("franktheunicorn.review.backends.get_backend") as mock_get_backend:
            mock_get_backend.return_value = _stub_backend("- First guidance.")
            guidance = distill_triage_guidance(project, operator_config_with_llm)
        assert guidance is not None
        assert guidance.guidance_text == "- First guidance."
        assert guidance.source_feedback_count == 2

        with patch("franktheunicorn.review.backends.get_backend") as mock_get_backend:
            mock_get_backend.return_value = _stub_backend("- Updated guidance.")
            distill_triage_guidance(project, operator_config_with_llm)

        assert SecurityTriageGuidance.objects.filter(project=project).count() == 1
        guidance.refresh_from_db()
        assert guidance.guidance_text == "- Updated guidance."

    def test_includes_project_and_global_feedback(
        self, operator_config_with_llm: OperatorConfig
    ) -> None:
        project = ProjectFactory()
        SecurityTriageFeedbackFactory(project=project, operator_comment="project-specific")
        SecurityTriageFeedbackFactory(project=None, operator_comment="global-comment")

        captured: dict[str, str] = {}

        def _capture_complete(prompt: str, *, system: str = "") -> str:
            captured["user"] = prompt
            return "- Guidance."

        with patch("franktheunicorn.review.backends.get_backend") as mock_get_backend:
            backend = MagicMock()
            backend.complete.side_effect = _capture_complete
            mock_get_backend.return_value = backend
            distill_triage_guidance(project, operator_config_with_llm)

        assert "project-specific" in captured["user"]
        assert "global-comment" in captured["user"]

    def test_call_failure_returns_none(self, operator_config_with_llm: OperatorConfig) -> None:
        project = ProjectFactory()
        SecurityTriageFeedbackFactory(project=project)
        with patch("franktheunicorn.review.backends.get_backend") as mock_get_backend:
            backend = MagicMock()
            backend.complete.side_effect = RuntimeError("llm down")
            mock_get_backend.return_value = backend
            assert distill_triage_guidance(project, operator_config_with_llm) is None


@pytest.mark.django_db
class TestResolveTriageGuidance:
    def test_no_guidance_returns_empty(self) -> None:
        project = ProjectFactory()
        assert resolve_triage_guidance(project) == ""
        assert resolve_triage_guidance(None) == ""

    def test_project_guidance_wins_over_global(self) -> None:
        project = ProjectFactory()
        SecurityTriageGuidanceFactory(project=None, guidance_text="global guidance")
        SecurityTriageGuidanceFactory(project=project, guidance_text="project guidance")

        assert resolve_triage_guidance(project) == "project guidance"

    def test_falls_back_to_global_guidance(self) -> None:
        project = ProjectFactory()
        SecurityTriageGuidanceFactory(project=None, guidance_text="global guidance")

        assert resolve_triage_guidance(project) == "global guidance"
        assert resolve_triage_guidance(None) == "global guidance"

    def test_inactive_guidance_ignored(self) -> None:
        project = ProjectFactory()
        SecurityTriageGuidanceFactory(project=project, guidance_text="stale", is_active=False)

        assert resolve_triage_guidance(project) == ""
