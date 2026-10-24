"""Tests for the LLM sub-check registry and runner."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig, ProjectConfig
from franktheunicorn.core.models import PullRequest
from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.checks import (
    BaseCheck,
    _get_registry,
    run_enabled_checks,
)
from franktheunicorn.review.prior_comments import PriorComment
from tests.factories import AntiPatternFactory


class TestRegistry:
    def test_coverage_registered(self) -> None:
        registry = _get_registry()
        assert "coverage" in registry

    def test_security_registered(self) -> None:
        registry = _get_registry()
        assert "security" in registry

    def test_security_context_registered(self) -> None:
        registry = _get_registry()
        assert "security-context" in registry

    def test_registry_values_are_base_check_subclasses(self) -> None:
        registry = _get_registry()
        for cls in registry.values():
            assert issubclass(cls, BaseCheck)

    def test_every_registry_key_is_known_to_config(self, caplog: pytest.LogCaptureFixture) -> None:
        # The config validator warns on llm_checks it doesn't know, and the
        # registry is the source of truth — a check that exists but isn't in
        # the known list warns on every project that enables it.
        import logging

        registry = _get_registry()
        with caplog.at_level(logging.WARNING, logger="franktheunicorn.config.models"):
            ProjectConfig(owner="apache", repo="spark", llm_checks=sorted(registry))
        assert not [r for r in caplog.records if "Unknown llm_check" in r.getMessage()], (
            f"registry keys missing from config known list: {[r.getMessage() for r in caplog.records]}"
        )


@pytest.mark.django_db
class TestRunEnabledChecks:
    def test_no_checks_configured_returns_empty(
        self,
        db_pr: PullRequest,
        spark_project_config: ProjectConfig,
        operator_config: OperatorConfig,
    ) -> None:
        """When llm_checks is empty, nothing runs."""
        assert spark_project_config.llm_checks == []
        drafts = run_enabled_checks(
            db_pr,
            "diff content",
            project_config=spark_project_config,
            operator_config=operator_config,
        )
        assert drafts == []

    def test_unknown_check_is_skipped(
        self,
        db_pr: PullRequest,
        operator_config: OperatorConfig,
    ) -> None:
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["nonexistent_check"],
        )
        drafts = run_enabled_checks(
            db_pr,
            "diff content",
            project_config=config,
            operator_config=operator_config,
        )
        assert drafts == []

    def test_each_check_records_its_run(self, db_pr: PullRequest) -> None:
        """A clean check and a crashed one both leave a record, so neither
        reads as "not run" on the dashboard."""
        config = ProjectConfig(owner="apache", repo="spark", llm_checks=["coverage", "security"])
        op_config = OperatorConfig(llm_backends=[LLMBackendConfig(provider="stub")])

        def fake(check: object, *args: object) -> list[ReviewFinding]:
            if check.__class__.__name__.lower().startswith("security"):
                raise RuntimeError("boom")
            return []

        with patch("franktheunicorn.review.checks._run_single_check", side_effect=fake):
            run_enabled_checks(db_pr, "diff", project_config=config, operator_config=op_config)

        db_pr.refresh_from_db()
        assert db_pr.agent_runs["check:coverage"]["status"] == "ok"
        assert db_pr.agent_runs["check:coverage"]["findings"] == 0
        assert db_pr.agent_runs["check:security"]["status"] == "failed"

    def test_coverage_check_produces_drafts_with_stub(
        self,
        db_pr: PullRequest,
    ) -> None:
        """Coverage check should produce correctly categorized drafts from findings."""
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["coverage"],
            test_expectations="tests required for all new features",
        )
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[
                ReviewFinding(
                    file_path="src/main.py",
                    line_number=10,
                    title="test-coverage: missing test",
                    body="No test for new function.",
                    confidence=0.8,
                    severity="important",
                ),
            ],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "+++ b/src/main.py\n+def new_func():\n+    pass",
                project_config=config,
                operator_config=op_config,
            )

        assert len(drafts) == 1
        assert "check:coverage" in drafts[0].sources
        assert drafts[0].file_path == "src/main.py"
        assert drafts[0].category == "test-coverage"

    def test_security_check_produces_drafts_with_stub(
        self,
        db_pr: PullRequest,
    ) -> None:
        """Security check should produce correctly categorized drafts from findings."""
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["security"],
        )
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[
                ReviewFinding(
                    file_path="src/auth.py",
                    line_number=42,
                    title="security: hardcoded secret",
                    body="API key is hardcoded.",
                    confidence=0.9,
                    severity="critical",
                ),
            ],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "+++ b/src/auth.py\n+API_KEY = 'sk-1234'",
                project_config=config,
                operator_config=op_config,
            )

        assert len(drafts) == 1
        assert "check:security" in drafts[0].sources
        assert drafts[0].file_path == "src/auth.py"
        assert drafts[0].category == "security"

    def test_security_context_check_produces_drafts_with_stub(
        self,
        db_pr: PullRequest,
    ) -> None:
        """Security-context check should produce drafts with category=security-context."""
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["security-context"],
        )
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[
                ReviewFinding(
                    file_path="src/middleware.py",
                    line_number=15,
                    title="security-context: CSRF middleware removed",
                    body="Removing CSRF middleware weakens security.",
                    confidence=0.85,
                    severity="critical",
                ),
            ],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "+++ b/src/middleware.py\n-CSRF_MIDDLEWARE = True",
                project_config=config,
                operator_config=op_config,
            )

        assert len(drafts) == 1
        assert "check:security-context" in drafts[0].sources
        assert drafts[0].file_path == "src/middleware.py"
        assert drafts[0].category == "security-context"

    def test_check_findings_go_through_antipattern_gating(
        self,
        db_pr: PullRequest,
    ) -> None:
        """Findings that match an anti-pattern should be suppressed."""
        AntiPatternFactory(
            pattern_text="No test for new function",
            project=db_pr.project,
        )

        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["coverage"],
        )
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[
                ReviewFinding(
                    file_path="src/main.py",
                    line_number=10,
                    title="test-coverage: missing test",
                    body="No test for new function.",
                    confidence=0.8,
                    severity="important",
                ),
            ],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "some diff",
                project_config=config,
                operator_config=op_config,
            )

        assert drafts == []

    def test_defaults_to_stub_without_operator_config(
        self,
        db_pr: PullRequest,
    ) -> None:
        """Should work without operator_config (falls back to stub)."""
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["coverage"],
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "some diff",
                project_config=config,
            )

        assert drafts == []

    def test_check_failure_does_not_crash(
        self,
        db_pr: PullRequest,
        operator_config: OperatorConfig,
    ) -> None:
        """If a check raises, it's caught and other checks continue."""
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            llm_checks=["coverage"],
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            side_effect=RuntimeError("LLM exploded"),
        ):
            drafts = run_enabled_checks(
                db_pr,
                "some diff",
                project_config=config,
                operator_config=operator_config,
            )

        assert drafts == []


@pytest.mark.django_db
class TestChecksSeePriorComments:
    """The checks are a finding source too, so the +1 rule has to reach them.

    A ``security:`` check restating a reviewer's own point is exactly the
    second comment the feature exists to stop.
    """

    def _config(self) -> ProjectConfig:
        return ProjectConfig(owner="apache", repo="spark", llm_checks=["security"])

    def _comment(self) -> PriorComment:
        return PriorComment(
            author="cloud-fan",
            body="This path joins user input without checking it escapes the dir.",
            file_path="src/main.py",
            line=10,
            url="https://github.com/apache/spark/pull/1#discussion_r1",
            disputed_by=("carol",),
        )

    def _finding(self) -> ReviewFinding:
        return ReviewFinding(
            file_path="src/main.py",
            line_number=10,
            title="security: path traversal",
            body="This path joins user input without checking it escapes the dir.",
            confidence=0.8,
            severity="important",
        )

    def test_the_prompt_carries_them(self, db_pr: PullRequest) -> None:
        op_config = OperatorConfig(llm_backends=[LLMBackendConfig(provider="stub")])
        seen: dict[str, str] = {}

        def capture(check: object, pr: object, diff: str, ctx: object, backend: object) -> list:
            seen["prior"] = ctx.prior_comments  # type: ignore[attr-defined]
            return []

        with patch("franktheunicorn.review.checks._run_single_check", side_effect=capture):
            run_enabled_checks(
                db_pr,
                "diff",
                project_config=self._config(),
                operator_config=op_config,
                prior_comments=[self._comment()],
            )

        assert "@cloud-fan" in seen["prior"]

    def test_a_restating_finding_becomes_a_plus_one(self, db_pr: PullRequest) -> None:
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
            github_username="holdenk",
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[self._finding()],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "diff",
                project_config=self._config(),
                operator_config=op_config,
                prior_comments=[self._comment()],
            )

        assert len(drafts) == 1
        assert drafts[0].comment_body.startswith("+1 @cloud-fan")

    def test_our_own_earlier_comment_drops_the_finding(self, db_pr: PullRequest) -> None:
        op_config = OperatorConfig(
            llm_backends=[LLMBackendConfig(provider="stub")],
            github_username="holdenk",
        )
        ours = PriorComment(
            author="holdenk",
            body="This path joins user input without checking it escapes the dir.",
            file_path="src/main.py",
            line=10,
        )

        with patch(
            "franktheunicorn.review.checks._run_single_check",
            return_value=[self._finding()],
        ):
            drafts = run_enabled_checks(
                db_pr,
                "diff",
                project_config=self._config(),
                operator_config=op_config,
                prior_comments=[ours],
            )

        assert drafts == []


class _FakeMeteredBackend:
    """Stands in for a BaseLLMBackend: scripted metered_call responses."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def metered_call(self, system: str, user: str, **kwargs: object) -> str:
        self.calls.append(user)
        return self._responses.pop(0) if self._responses else ""


class _FakePR:
    """Just the attrs _metered_call_with_retry reads off the PR."""

    project_id = 1
    pk = 1


class TestEmptyCompletionRetry:
    """A 200 with empty content is a real backend behaviour (qwen3.8 with
    thinking mode spends the output budget on reasoning). It used to parse to
    zero findings and record a CLEAN check — a check that never answered."""

    def _run(self, responses: list[str]) -> str:
        from franktheunicorn.review.checks import _metered_call_with_retry

        backend = _FakeMeteredBackend(responses)
        cfg = LLMBackendConfig(provider="openai", model="qwen3.8-27b")
        return _metered_call_with_retry(
            backend,
            cfg,
            "system",
            "user",
            check_name="coverage",
            pr=_FakePR(),
        )

    def test_empty_then_answer_retries_once(self) -> None:
        assert self._run(["", '{"findings": []}']) == '{"findings": []}'

    def test_nonempty_first_try_no_retry(self) -> None:
        backend = _FakeMeteredBackend(["text"])
        cfg = LLMBackendConfig(provider="openai", model="qwen3.8-27b")
        from franktheunicorn.review.checks import _metered_call_with_retry

        out = _metered_call_with_retry(
            backend,
            cfg,
            "system",
            "user",
            check_name="coverage",
            pr=_FakePR(),
        )
        assert out == "text"
        assert len(backend.calls) == 1

    def test_empty_twice_raises(self) -> None:
        from franktheunicorn.review.checks import _metered_call_with_retry

        backend = _FakeMeteredBackend(["", ""])
        cfg = LLMBackendConfig(provider="openai", model="qwen3.8-27b")
        with pytest.raises(RuntimeError, match="empty completion"):
            _metered_call_with_retry(
                backend,
                cfg,
                "system",
                "user",
                check_name="coverage",
                pr=_FakePR(),
            )
        assert len(backend.calls) == 2


class TestDiffCap:
    """The checks run on the first configured backend; a monster diff made
    the prompt unanswerable. The cap keeps the whole prompt bounded and says
    so in the prompt itself."""

    def _ctx(self, full: str = "", imported: str = "", prior: str = "") -> object:
        from tests.conftest import make_pr_context

        return make_pr_context(
            full_file_context=full, imported_modules_context=imported, prior_comments=prior
        )

    def test_small_diff_untouched(self) -> None:
        from franktheunicorn.review.checks import _cap_diff_for_checks

        diff = "+ a\n" * 100
        assert _cap_diff_for_checks(diff, self._ctx()) is diff

    def test_huge_diff_truncated_with_marker(self) -> None:
        from franktheunicorn.review.checks import (
            _MAX_CHECK_PROMPT_CHARS,
            _cap_diff_for_checks,
        )

        diff = "+ line\n" * 40_000  # ~280K chars
        capped = _cap_diff_for_checks(diff, self._ctx())
        assert len(capped) < len(diff)
        assert "[...diff truncated" in capped
        # Bounded by the cap (allowing the marker itself).
        assert len(capped) <= _MAX_CHECK_PROMPT_CHARS

    def test_context_shrinks_the_diff_room_but_not_below_floor(self) -> None:
        from franktheunicorn.review.checks import (
            _MIN_DIFF_ROOM_CHARS,
            _cap_diff_for_checks,
        )

        big_context = "x" * 110_000
        diff = "+ line\n" * 40_000
        capped = _cap_diff_for_checks(diff, self._ctx(full=big_context))
        # Context ate most of the budget; the diff still keeps its floor.
        assert len(capped) >= _MIN_DIFF_ROOM_CHARS
        assert "[...diff truncated" in capped

    def test_truncation_lands_on_a_line_boundary(self) -> None:
        from franktheunicorn.review.checks import _cap_diff_for_checks

        diff = ("+ " + "y" * 99 + "\n") * 40_000
        capped = _cap_diff_for_checks(diff, self._ctx())
        body = capped.split("\n[...diff truncated", 1)[0]
        assert body.endswith("\n")  # cut between lines, not mid-line

    @pytest.mark.django_db
    def test_dispatch_and_drafts_get_the_full_diff(self, db_pr: PullRequest) -> None:
        """The cap lives in the prompt path only. scan()-based checks (one is
        a security pre-filter that reads the diff) and draft creation (which
        re-parses the diff) must see the WHOLE diff — truncating them would
        blind a security control and break code-context extraction."""
        config = ProjectConfig(owner="apache", repo="spark", llm_checks=["coverage"])
        op_config = OperatorConfig(llm_backends=[LLMBackendConfig(provider="stub")])
        monster = "+ line\n" * 40_000
        seen: dict[str, str] = {}

        def fake_dispatch(
            check: object, pr: object, diff: str, *args: object
        ) -> list[ReviewFinding]:
            seen["diff"] = diff
            return []

        with patch("franktheunicorn.review.checks._run_check_dispatch", side_effect=fake_dispatch):
            run_enabled_checks(db_pr, monster, project_config=config, operator_config=op_config)
        assert seen["diff"] is monster  # uncapped, unmarker-ed

    def test_prompt_path_caps_the_diff(self) -> None:
        """_run_single_check caps before build_prompt, so the model's prompt
        is bounded and the model is told what it did not see."""
        from franktheunicorn.review.backends.base import BaseLLMBackend
        from franktheunicorn.review.checks import (
            _MAX_CHECK_PROMPT_CHARS,
            _run_single_check,
        )
        from franktheunicorn.review.checks.coverage import CoverageCheck
        from tests.conftest import make_pr_context

        class _RecordingBackend(BaseLLMBackend):
            def __init__(self) -> None:
                from franktheunicorn.config.models import LLMBackendConfig as Cfg

                super().__init__(Cfg(provider="openai", model="qwen"))
                self.user_messages: list[str] = []

            def _call_api(
                self, system_prompt: str, user_message: str, api_key: str
            ) -> str:  # pragma: no cover - not reached; metered_call is patched
                raise AssertionError

        monster = "+ line\n" * 40_000
        ctx = make_pr_context()
        check = CoverageCheck()
        backend = _RecordingBackend()
        backend.metered_call = lambda system, user, **kw: (  # type: ignore[method-assign]
            backend.user_messages.append(user) or '{"findings": []}'
        )
        cfg = LLMBackendConfig(provider="openai", model="qwen")
        with patch("franktheunicorn.review.backends.get_backend", return_value=backend):
            _run_single_check(check, _FakePR(), monster, ctx, cfg)  # type: ignore[arg-type]
        assert len(backend.user_messages) == 1
        assert "[...diff truncated" in backend.user_messages[0]
        assert len(backend.user_messages[0]) <= _MAX_CHECK_PROMPT_CHARS + 2_000


class TestContextPassthrough:
    @pytest.mark.django_db
    def test_checks_context_receives_repo_path_and_real_diff(
        self, db_pr: PullRequest, tmp_path: Path
    ) -> None:
        """The checks path used to pass neither: no surrounding code at all,
        on the pass whose whole job is reading around the change. Pin both,
        so a refactor of run_enabled_checks cannot silently drop them again."""
        from tests.conftest import make_pr_context

        seen: dict[str, object] = {}

        def spy(pr: object, pc: object, op: object, **kwargs: object) -> object:
            seen.update(kwargs)
            return make_pr_context()

        config = ProjectConfig(owner="apache", repo="spark", llm_checks=["coverage"])
        op_config = OperatorConfig(llm_backends=[LLMBackendConfig(provider="stub")])
        with (
            patch("franktheunicorn.review.checks.build_pr_context", side_effect=spy),
            patch("franktheunicorn.review.checks._run_check_dispatch", return_value=[]),
        ):
            run_enabled_checks(
                db_pr,
                "the real diff",
                project_config=config,
                operator_config=op_config,
                repo_path=tmp_path,
            )
        assert seen.get("diff") == "the real diff"
        assert seen.get("repo_path") == tmp_path
