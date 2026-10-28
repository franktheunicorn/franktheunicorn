"""Tests for the review-guidance builder.

Mirrors the triage-learning tests: gates decline with a reason, the build
walks valid reports with a dated introducing commit, per-commit notes are
collected and distilled, and the result is stored as a SecurityReviewGuidance
row that the introduced-CVE scan injects. ``resolve_review_guidance`` follows
the same project-over-global precedence as triage guidance.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.models import (
    LLMBackendConfig,
    OperatorConfig,
    SecurityTriageConfig,
)
from franktheunicorn.core.models import SecurityReviewGuidance
from franktheunicorn.security.review_guidance import (
    _extract_text,
    build_review_guidance,
    resolve_review_guidance,
)
from tests.factories import ProjectFactory, SecurityReportFactory


# The distillation path sleeps for a 30s cooldown + 30s retries against the
# rate-limited proxy. Patch it out for every test so the suite stays fast.
@pytest.fixture(autouse=True)
def _no_sleep() -> Any:
    with patch("franktheunicorn.security.review_guidance.time.sleep"):
        yield


def _operator(*, llm: bool = True, reviewer: str = "claude") -> OperatorConfig:
    return OperatorConfig(
        github_username="holdenk",
        llm_backends=[LLMBackendConfig(provider="stub")] if llm else [],
        security_triage=SecurityTriageConfig(
            enabled=True,
            verifier=_verifier(reviewer=reviewer),
        ),
    )


def _verifier(*, reviewer: str = "claude"):
    from franktheunicorn.config.models import SecurityVerifierConfig

    return SecurityVerifierConfig(enabled=True, reviewer=reviewer)


def _stub_backend(response: str) -> MagicMock:
    backend = MagicMock()
    backend.complete.return_value = response
    return backend


def _executor_with(diffs: dict[str, str]) -> MagicMock:
    """A fake executor whose ``run`` returns the diff for ``git show <commit>``."""
    executor = MagicMock()
    executor.cwd = "/w/spark"
    executor.subdir = ""

    def prepare_repo(owner: str, repo: str, **kwargs: Any) -> str:
        return "/w/spark"

    executor.prepare_repo = MagicMock(side_effect=prepare_repo)

    def run(cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
        from franktheunicorn.review.tool_executor import unwrap_guarded

        cmd = unwrap_guarded(cmd)
        if cmd[:2] == ["git", "show"]:
            commit = cmd[2] if len(cmd) > 2 else ""
            diff = diffs.get(commit, "diff --git a/f b/f\n+bad code")
            return _ok(diff)
        if cmd[:2] == ["git", "symbolic-ref"]:
            return _ok("origin/master\n")
        if cmd[:2] == ["git", "checkout"]:
            return _ok("")
        if cmd[:2] == ["git", "clean"]:
            return _ok("")
        if cmd[:2] == ["git", "rev-parse"]:
            return _ok("deadbeefcafe\n")
        return _ok("")

    executor.run = MagicMock(side_effect=run)
    return executor


def _ok(stdout: str = "") -> MagicMock:
    r = MagicMock()
    r.ok = True
    r.returncode = 0
    r.stdout = stdout
    r.stderr = ""
    return r


def _valid_report(
    commit: str, *, project: Any = None, title: str = "Off-by-one", cve: str = ""
) -> Any:
    return SecurityReportFactory(
        project=project,
        status="valid",
        introduced_commit=commit,
        title=title,
        matched_cve_id=cve,
    )


@pytest.mark.django_db
class TestGateDeclines:
    """Every early return carries a reason; "didn't run" must not look like "ran clean"."""

    def test_verifier_disabled_does_not_run(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        config = _operator()
        config.security_triage.verifier = _verifier()
        config.security_triage.verifier.enabled = False
        result = build_review_guidance(project, config)
        assert "verifier.enabled is false" in result.error
        assert not SecurityReviewGuidance.objects.filter(project=project).exists()

    def test_no_llm_backends_does_not_run(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        result = build_review_guidance(project, _operator(llm=False))
        assert "no llm_backends" in result.error
        assert not SecurityReviewGuidance.objects.filter(project=project).exists()

    def test_no_valid_reports_with_introduced_commit(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        # A valid report with no introduced_commit, and a non-valid report with one.
        SecurityReportFactory(project=project, status="valid", introduced_commit="")
        SecurityReportFactory(project=project, status="invalid", introduced_commit="abc123")
        result = build_review_guidance(project, _operator())
        assert "no valid reports with a dated introducing commit" in result.error

    def test_no_resolvable_reviewer_does_not_run(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        _valid_report("abc123", project=project)
        config = _operator()
        config.security_triage.verifier = _verifier(reviewer="nobody")
        result = build_review_guidance(project, config)
        assert "nobody" in result.error


@pytest.mark.django_db
class TestBuild:
    """The happy path: per-commit notes are collected, distilled, and stored."""

    def test_builds_and_stores_guidance(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        _valid_report(
            "aaa111", project=project, title="SQL injection in parser", cve="CVE-2026-1111"
        )
        _valid_report(
            "bbb222", project=project, title="Unsafe deserialization", cve="CVE-2026-2222"
        )
        executor = _executor_with({"aaa111": "diff aaa", "bbb222": "diff bbb"})
        backend = _stub_backend("- Watch for unparameterized SQL.\n- Check deserializers.")
        with (
            patch("franktheunicorn.review.backends.get_backend", return_value=backend),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            result = build_review_guidance(project, _operator())
        assert result.error == ""
        assert result.commit_count == 2
        assert "Watch for unparameterized SQL" in result.guidance_text
        guidance = SecurityReviewGuidance.objects.get(project=project, is_active=True)
        assert guidance.guidance_text == result.guidance_text
        assert guidance.source_report_count == 2
        assert guidance.source_commit_count == 2

    def test_dry_run_does_not_store(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        _valid_report("aaa111", project=project)
        executor = _executor_with({"aaa111": "diff"})
        backend = _stub_backend("- A bullet.")
        with (
            patch("franktheunicorn.review.backends.get_backend", return_value=backend),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            result = build_review_guidance(project, _operator(), dry_run=True)
        assert result.error == ""
        assert not SecurityReviewGuidance.objects.filter(project=project).exists()

    def test_duplicate_commits_are_deduped(self) -> None:
        """Two reports citing the same introducing commit = one git show, one note."""
        project = ProjectFactory(owner="apache", repo="spark")
        _valid_report("aaa111", project=project, title="First finding")
        _valid_report("aaa111", project=project, title="Second finding, same commit")
        executor = _executor_with({"aaa111": "diff"})
        backend = _stub_backend("- One note for the shared commit.")
        with (
            patch("franktheunicorn.review.backends.get_backend", return_value=backend),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            result = build_review_guidance(project, _operator())
        assert result.commit_count == 1
        assert result.report_count == 2

    def test_max_commits_caps_the_count(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        for i in range(5):
            _valid_report(f"commit{i:06d}", project=project)
        executor = _executor_with({})
        backend = _stub_backend("- bullet")
        with (
            patch("franktheunicorn.review.backends.get_backend", return_value=backend),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            result = build_review_guidance(project, _operator(), max_commits=3)
        assert result.report_count == 3

    def test_a_failed_git_show_is_skipped_not_fatal(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        _valid_report("aaa111", project=project)

        # An executor where git show fails but everything else succeeds.
        executor = MagicMock()
        executor.cwd = "/w/spark"
        executor.prepare_repo = MagicMock(return_value="/w/spark")
        bad_show = MagicMock()
        bad_show.ok = False
        bad_show.returncode = 1
        bad_show.stderr = "no such commit"
        bad_show.stdout = ""

        def run(cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
            from franktheunicorn.review.tool_executor import unwrap_guarded

            cmd = unwrap_guarded(cmd)
            if cmd[:2] == ["git", "show"]:
                return bad_show
            if cmd[:2] == ["git", "symbolic-ref"]:
                return _ok("origin/master\n")
            if cmd[:2] == ["git", "rev-parse"]:
                return _ok("deadbeefcafe\n")
            return _ok("")

        executor.run = MagicMock(side_effect=run)
        backend = _stub_backend("- guidance")
        with (
            patch("franktheunicorn.review.backends.get_backend", return_value=backend),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            result = build_review_guidance(project, _operator())
        # The failed show means no note; distillation gets nothing -> error.
        assert "no per-commit notes" in result.error

    def test_a_failed_per_commit_call_is_skipped(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        _valid_report("aaa111", project=project, title="good")
        _valid_report("bbb222", project=project, title="bad")
        executor = _executor_with({"aaa111": "diff a", "bbb222": "diff b"})
        backend = MagicMock()
        # First per-commit call raises, second succeeds, distill succeeds.
        backend.complete.side_effect = [RuntimeError("boom"), "- note for good", "- distilled"]
        with (
            patch("franktheunicorn.review.backends.get_backend", return_value=backend),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            result = build_review_guidance(project, _operator())
        assert result.error == ""
        assert "distilled" in result.guidance_text

    def test_a_failed_distillation_is_an_error(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        _valid_report("aaa111", project=project)
        executor = _executor_with({"aaa111": "diff"})
        backend = MagicMock()
        # Per-commit succeeds; every distillation attempt raises.
        backend.complete.side_effect = [
            "- note",
            RuntimeError("boom"),
            RuntimeError,
            RuntimeError,
            RuntimeError,
            RuntimeError,
        ]
        with (
            patch("franktheunicorn.review.backends.get_backend", return_value=backend),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            result = build_review_guidance(project, _operator())
        assert "distillation call failed" in result.error

    def test_empty_distillation_output_is_an_error(self) -> None:
        project = ProjectFactory(owner="apache", repo="spark")
        _valid_report("aaa111", project=project)
        executor = _executor_with({"aaa111": "diff"})
        backend = MagicMock()
        # Per-commit returns a note; every distillation attempt returns whitespace.
        backend.complete.side_effect = ["- a real note", "   ", "   ", "   ", "   ", "   "]
        with (
            patch("franktheunicorn.review.backends.get_backend", return_value=backend),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            result = build_review_guidance(project, _operator())
        assert "failed after retries" in result.error

    def test_distillation_returning_error_json_is_not_stored(self) -> None:
        """The openai-compatible backend returns an error body as text rather
        than raising. A rate-limit message must not be stored as guidance.
        """
        project = ProjectFactory(owner="apache", repo="spark")
        _valid_report("aaa111", project=project)
        executor = _executor_with({"aaa111": "diff"})
        backend = MagicMock()
        backend.complete.side_effect = [
            "- a real note about the diff",
            '{"error": {"message": "Your resource has become unavailable"}}',
            '{"error": {"message": "rate limited"}}',
            '{"error": {"message": "rate limited"}}',
            '{"error": {"message": "rate limited"}}',
            '{"error": {"message": "rate limited"}}',
        ]
        with (
            patch("franktheunicorn.review.backends.get_backend", return_value=backend),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            result = build_review_guidance(project, _operator())
        assert "failed after retries" in result.error
        assert not SecurityReviewGuidance.objects.filter(project=project).exists()

    def test_distillation_succeeds_on_retry(self) -> None:
        """A transient rate-limit on the first distillation attempt should not
        waste the per-commit work — the retry succeeds and stores guidance.
        """
        project = ProjectFactory(owner="apache", repo="spark")
        _valid_report("aaa111", project=project)
        executor = _executor_with({"aaa111": "diff"})
        backend = MagicMock()
        backend.complete.side_effect = [
            "- a real note",
            '{"error": {"message": "rate limited"}}',
            "- real distilled guidance\n- check for X",
        ]
        with (
            patch("franktheunicorn.review.backends.get_backend", return_value=backend),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            result = build_review_guidance(project, _operator())
        assert result.error == ""
        assert "real distilled guidance" in result.guidance_text
        assert SecurityReviewGuidance.objects.filter(project=project, is_active=True).count() == 1

    def test_json_wrapped_responses_are_unwrapped(self) -> None:
        """The backend forces response_format=json_object, so the model returns
        JSON even when the prompt asked for plain bullets. _extract_text pulls
        the readable text out so stored guidance is plain, not JSON.
        """
        project = ProjectFactory(owner="apache", repo="spark")
        _valid_report("aaa111", project=project)
        executor = _executor_with({"aaa111": "diff"})
        backend = MagicMock()
        backend.complete.side_effect = [
            '{"analysis": "Check for eval of user input in new handlers."}',
            '{"guidance": ["Validate all inputs.", "Watch for unsafe deserialization."]}',
        ]
        with (
            patch("franktheunicorn.review.backends.get_backend", return_value=backend),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            result = build_review_guidance(project, _operator())
        assert result.error == ""
        # The per-commit note is unwrapped (no JSON braces in the stored note).
        assert "{" not in result.notes[0]
        assert "eval of user input" in result.notes[0]
        # The distilled guidance is unwrapped too.
        assert "Validate all inputs." in result.guidance_text
        assert "unsafe deserialization" in result.guidance_text.lower()
        assert "{" not in result.guidance_text


class TestExtractText:
    """_extract_text unwraps JSON-wrapped LLM responses."""

    def test_plain_text_passes_through(self) -> None:
        assert _extract_text("- a plain bullet\n- another") == "- a plain bullet\n- another"

    def test_dict_with_string_value(self) -> None:
        assert _extract_text('{"analysis": "look for eval"}') == "look for eval"

    def test_dict_with_list_value(self) -> None:
        result = _extract_text('{"guidance": ["bullet one", "bullet two"]}')
        assert "bullet one" in result
        assert "bullet two" in result

    def test_invalid_json_passes_through(self) -> None:
        assert _extract_text("{not really json") == "{not really json"

    def test_empty_dict_returns_original(self) -> None:
        assert _extract_text("{}") == "{}"


@pytest.mark.django_db
class TestResolveReviewGuidance:
    """Project-over-global precedence, mirroring triage guidance."""

    def test_no_guidance_is_empty_string(self) -> None:
        project = ProjectFactory()
        assert resolve_review_guidance(project) == ""

    def test_project_guidance_wins_over_global(self) -> None:
        project = ProjectFactory()
        SecurityReviewGuidance.objects.create(
            project=project, guidance_text="project-specific", is_active=True
        )
        SecurityReviewGuidance.objects.create(project=None, guidance_text="global", is_active=True)
        assert resolve_review_guidance(project) == "project-specific"

    def test_global_guidance_is_the_fallback(self) -> None:
        project = ProjectFactory()
        SecurityReviewGuidance.objects.create(project=None, guidance_text="global", is_active=True)
        assert resolve_review_guidance(project) == "global"

    def test_inactive_guidance_is_ignored(self) -> None:
        project = ProjectFactory()
        SecurityReviewGuidance.objects.create(
            project=project, guidance_text="stale", is_active=False
        )
        assert resolve_review_guidance(project) == ""

    def test_a_rebuild_replaces_the_active_row(self) -> None:
        """A second build upserts the active row rather than stacking a second."""
        project = ProjectFactory(owner="apache", repo="spark")
        SecurityReviewGuidance.objects.create(
            project=project, guidance_text="old guidance", is_active=True, source_report_count=1
        )
        _valid_report("aaa111", project=project)
        executor = _executor_with({"aaa111": "diff"})
        backend = _stub_backend("- new guidance")
        with (
            patch("franktheunicorn.review.backends.get_backend", return_value=backend),
            patch("franktheunicorn.review.tool_executor.make_executor", return_value=executor),
        ):
            build_review_guidance(project, _operator())
        assert SecurityReviewGuidance.objects.filter(project=project, is_active=True).count() == 1
        guidance = SecurityReviewGuidance.objects.get(project=project, is_active=True)
        assert "new guidance" in guidance.guidance_text


@pytest.mark.django_db
class TestPromptInjection:
    """The introduced-CVE scan prompt includes the review guidance slot."""

    def test_the_prompt_carries_the_guidance(self) -> None:
        from franktheunicorn.security.introduced_scan import _build_prompt

        prompt = _build_prompt(
            project="apache/spark",
            branch="master",
            commit_range="last 2 days",
            accepted_context="(none)",
            rejected_context="(none)",
            security_model="(none)",
            review_guidance="- Watch for unparameterized SQL.",
            diff="diff",
            magpie_skills="",
            addendum="",
        )
        assert "Watch for unparameterized SQL" in prompt
        assert "REVIEW GUIDANCE" in prompt
