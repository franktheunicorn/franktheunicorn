"""Tests for the second-level review judge (review/judge.py)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from franktheunicorn.config.models import LLMBackendConfig
from franktheunicorn.review.backends.base import PRContext, ReviewFinding
from franktheunicorn.review.judge import (
    JudgeVerdict,
    apply_verdicts,
    judge_findings,
)


class _FakeBackend:
    """A backend whose metered_call returns queued responses in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, str]] = []

    def metered_call(
        self,
        system_prompt: str,
        user_message: str,
        *,
        action_type: str,
        project_id: int | None = None,
        pr_id: int | None = None,
        api_key: str | None = None,
    ) -> str:
        self.calls.append((action_type, system_prompt, user_message))
        return self._responses.pop(0) if self._responses else "[]"


def _finding(body: str, file_path: str = "a.py", line: int | None = 10) -> ReviewFinding:
    return ReviewFinding(file_path=file_path, line_number=line, title=body[:20], body=body)


def _ctx() -> PRContext:
    return PRContext(
        pr_title="t",
        pr_body="b",
        pr_author="alice",
        pr_number=1,
        project_name="apache/spark",
        review_context="",
        review_style="",
        tone="",
        test_expectations="",
        governance="standard",
    )


def _backend_cfg() -> LLMBackendConfig:
    return LLMBackendConfig(provider="openai", model="qwen3-8b")


class TestJudgeFindings:
    def test_empty_findings_returns_empty(self) -> None:
        assert judge_findings([], _ctx(), _backend_cfg()) == []

    def test_no_backend_keeps_all(self) -> None:
        findings = [_finding("x"), _finding("y")]
        # stub provider resolves to no metered backend
        verdicts = judge_findings(findings, _ctx(), LLMBackendConfig(provider="stub"))
        assert all(v.keep for v in verdicts)
        assert len(verdicts) == 2

    def test_none_backend_keeps_all(self) -> None:
        findings = [_finding("x")]
        verdicts = judge_findings(findings, _ctx(), None)
        assert verdicts[0].keep

    def test_batch_drops_a_finding(self) -> None:
        findings = [_finding("real bug"), _finding("vague nit")]
        batch = json.dumps(
            [
                {
                    "index": 0,
                    "keep": True,
                    "reason": "specific",
                    "revised": "",
                    "borderline": False,
                },
                {"index": 1, "keep": False, "reason": "vague", "revised": "", "borderline": False},
            ]
        )
        fake = _FakeBackend([batch])
        with patch("franktheunicorn.review.judge._get_metered_backend", return_value=fake):
            verdicts = judge_findings(findings, _ctx(), _backend_cfg())
        assert verdicts[0].keep is True
        assert verdicts[1].keep is False

    def test_batch_revises_body(self) -> None:
        findings = [_finding("is not free of side effects here")]
        batch = json.dumps(
            [
                {
                    "index": 0,
                    "keep": True,
                    "reason": "fix fragment",
                    "revised": "requiredChildDistribution is not free of side effects here.",
                    "borderline": False,
                }
            ]
        )
        fake = _FakeBackend([batch])
        with patch("franktheunicorn.review.judge._get_metered_backend", return_value=fake):
            verdicts = judge_findings(findings, _ctx(), _backend_cfg())
        assert verdicts[0].revised_body.startswith("requiredChildDistribution")

    def test_nonsense_revision_is_discarded(self) -> None:
        findings = [_finding("real point")]
        batch = json.dumps(
            [{"index": 0, "keep": True, "reason": "", "revised": "{}", "borderline": False}]
        )
        fake = _FakeBackend([batch])
        with patch("franktheunicorn.review.judge._get_metered_backend", return_value=fake):
            verdicts = judge_findings(findings, _ctx(), _backend_cfg())
        assert verdicts[0].revised_body == ""

    def test_borderline_triggers_verify_and_can_flip(self) -> None:
        findings = [_finding("maybe a race here")]
        batch = json.dumps(
            [{"index": 0, "keep": True, "reason": "unsure", "revised": "", "borderline": True}]
        )
        verify = json.dumps({"keep": False, "reason": "already guarded by the lock"})
        fake = _FakeBackend([batch, verify])
        with patch("franktheunicorn.review.judge._get_metered_backend", return_value=fake):
            verdicts = judge_findings(findings, _ctx(), _backend_cfg(), verify_borderline=True)
        assert verdicts[0].keep is False
        assert verdicts[0].tier == "verify"
        # One batch + one verify call.
        assert [c[0] for c in fake.calls] == ["judge-batch", "judge-verify"]

    def test_verify_disabled_keeps_borderline(self) -> None:
        findings = [_finding("maybe a race here")]
        batch = json.dumps(
            [{"index": 0, "keep": True, "reason": "unsure", "revised": "", "borderline": True}]
        )
        fake = _FakeBackend([batch])
        with patch("franktheunicorn.review.judge._get_metered_backend", return_value=fake):
            verdicts = judge_findings(findings, _ctx(), _backend_cfg(), verify_borderline=False)
        assert verdicts[0].keep is True
        assert [c[0] for c in fake.calls] == ["judge-batch"]

    def test_max_verify_caps_tier_b_calls(self) -> None:
        findings = [_finding(f"f{i}") for i in range(4)]
        batch = json.dumps(
            [
                {"index": i, "keep": True, "reason": "", "revised": "", "borderline": True}
                for i in range(4)
            ]
        )
        # Two verify responses; cap is 2, so only two verify calls fire.
        verify = json.dumps({"keep": True, "reason": "ok"})
        fake = _FakeBackend([batch, verify, verify])
        with patch("franktheunicorn.review.judge._get_metered_backend", return_value=fake):
            judge_findings(findings, _ctx(), _backend_cfg(), verify_borderline=True, max_verify=2)
        verify_calls = [c for c in fake.calls if c[0] == "judge-verify"]
        assert len(verify_calls) == 2

    def test_max_reask_caps_unruled_reasks(self) -> None:
        # Batch rules on index 0 only (small-model behaviour); 1-3 are unruled.
        # The cap lets two re-asks fire; the rest are kept, never dropped.
        findings = [_finding(f"f{i}") for i in range(4)]
        batch = json.dumps([{"index": 0, "keep": False, "reason": "no", "revised": ""}])
        one = json.dumps({"keep": True, "reason": "ok"})
        fake = _FakeBackend([batch, one, one])
        with patch("franktheunicorn.review.judge._get_metered_backend", return_value=fake):
            verdicts = judge_findings(findings, _ctx(), _backend_cfg(), max_reask=2)
        one_calls = [c for c in fake.calls if c[0] == "judge-one"]
        assert len(one_calls) == 2
        assert verdicts[0].keep is False
        assert all(v.keep for v in verdicts[1:])
        assert verdicts[3].tier == "unruled"
        assert verdicts[3].reason == "re-ask cap reached"

    def test_unparseable_batch_keeps_all(self) -> None:
        findings = [_finding("x"), _finding("y")]
        fake = _FakeBackend(["not json at all"])
        with patch("franktheunicorn.review.judge._get_metered_backend", return_value=fake):
            verdicts = judge_findings(findings, _ctx(), _backend_cfg())
        assert all(v.keep for v in verdicts)

    def test_batch_call_exception_keeps_all(self) -> None:
        findings = [_finding("x")]

        class _Boom:
            def metered_call(self, *a: Any, **k: Any) -> str:
                raise RuntimeError("boom")

        with patch("franktheunicorn.review.judge._get_metered_backend", return_value=_Boom()):
            verdicts = judge_findings(findings, _ctx(), _backend_cfg())
        assert verdicts[0].keep

    def test_silent_finding_is_kept(self) -> None:
        # Judge returns a verdict only for index 0; index 1 must survive.
        findings = [_finding("x"), _finding("y")]
        batch = json.dumps(
            [{"index": 0, "keep": False, "reason": "no", "revised": "", "borderline": False}]
        )
        fake = _FakeBackend([batch])
        with patch("franktheunicorn.review.judge._get_metered_backend", return_value=fake):
            verdicts = judge_findings(findings, _ctx(), _backend_cfg())
        assert verdicts[0].keep is False
        assert verdicts[1].keep is True


class TestApplyVerdicts:
    def test_filters_and_applies_revision(self) -> None:
        findings = [_finding("keep me"), _finding("drop me"), _finding("revise me")]
        verdicts = [
            JudgeVerdict(keep=True),
            JudgeVerdict(keep=False),
            JudgeVerdict(keep=True, revised_body="revised text"),
        ]
        kept, indices = apply_verdicts(findings, verdicts)
        assert indices == [0, 2]
        assert kept[0].body == "keep me"
        assert kept[1].body == "revised text"

    def test_all_dropped(self) -> None:
        findings = [_finding("a")]
        kept, indices = apply_verdicts(findings, [JudgeVerdict(keep=False)])
        assert kept == []
        assert indices == []
