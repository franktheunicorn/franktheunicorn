"""Tests for mining ordinary hardening ideas from CVE reports."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.utils import timezone

from franktheunicorn.config.models import (
    AgentCLIReviewerConfig,
    HardeningIdeasConfig,
    OperatorConfig,
    SecurityVerifierConfig,
)
from franktheunicorn.core.models import HardeningIdea, SecurityReport
from franktheunicorn.review.tool_executor import ExecResult
from franktheunicorn.security.hardening_ideas import (
    _parse_ideas,
    cve_report_candidates,
    idea_fingerprint,
    mine_backlog,
    mine_report,
)
from tests.factories import ProjectFactory, SecurityReportFactory


def _hardening(**overrides: Any) -> HardeningIdeasConfig:
    base: dict[str, Any] = {"enabled": True, "max_reports_per_run": 5}
    base.update(overrides)
    return HardeningIdeasConfig(**base)


def _operator(*, hardening: HardeningIdeasConfig | None = None) -> OperatorConfig:
    config = OperatorConfig(mock_mode=True)
    config.security_triage.verifier = SecurityVerifierConfig(enabled=True, reviewer="claude")
    config.security_triage.hardening_ideas = hardening or _hardening()
    config.agent_cli_reviewers = [AgentCLIReviewerConfig(name="claude", cli_path="claude")]
    return config


class _FakeExecutor:
    def __init__(self, responses: dict[str, Any], cwd: str = "/w/spark") -> None:
        self.responses = responses
        self.cwd = cwd
        self.calls: list[list[str]] = []

    def prepare_repo(self, owner: str, repo: str, **kwargs: Any) -> str | None:
        return self.cwd

    def run(self, cmd: list[str], cwd: str, timeout: int = 0, stdin: Any = None) -> Any:
        from franktheunicorn.review.tool_executor import unwrap_guarded

        cmd = unwrap_guarded(cmd)
        self.calls.append(cmd)
        shape = " ".join(cmd[:4]) if cmd and cmd[0] == "git" else (cmd[0] if cmd else "")
        for key, response in self.responses.items():
            if key in shape:
                return response
        return ExecResult(returncode=0, stdout="", stderr="")


def _executor_with(agent_output: str) -> _FakeExecutor:
    return _FakeExecutor(
        {
            "fetch": ExecResult(returncode=0, stdout="", stderr=""),
            "symbolic-ref": ExecResult(returncode=0, stdout="origin/master\n", stderr=""),
            "checkout": ExecResult(returncode=0, stdout="", stderr=""),
            "clean": ExecResult(returncode=0, stdout="", stderr=""),
            "rev-parse HEAD": ExecResult(returncode=0, stdout="deadbeefcafe\n", stderr=""),
            "claude": ExecResult(returncode=0, stdout=agent_output, stderr=""),
        }
    )


def _idea_json(*ideas: dict[str, Any], summary: str = "looked fine") -> str:
    return json.dumps({"summary": summary, "ideas": list(ideas)})


class TestParseIdeas:
    def test_parses_and_scrubs_security_words(self) -> None:
        raw = _idea_json(
            {
                "title": "Tighten bounds check",
                "description": "Add a validation.",
                "category": "validation",
                "severity": "important",
                "file_path": "a.py",
                "still_exists": "yes",
                "still_exists_evidence": "line 12",
                "draft_ticket_title": "Fix security hole in parser",
                "draft_ticket_body": "This vulnerability needs a check.",
            }
        )
        summary, drafts = _parse_ideas(raw)
        assert summary == "looked fine"
        assert len(drafts) == 1
        assert "security" not in drafts[0].draft_ticket_title.lower()
        assert "vulnerabilit" not in drafts[0].draft_ticket_body.lower()

    def test_unparseable_is_empty(self) -> None:
        assert _parse_ideas("sorry, no json here") == ("", [])


@pytest.mark.django_db
class TestCandidates:
    def test_only_unscanned_cve_reports(self) -> None:
        project = ProjectFactory()
        hit = SecurityReportFactory(project=project, matched_cve_id="CVE-2024-1", status="valid")
        SecurityReportFactory(project=project, matched_cve_id="", status="valid")  # no CVE
        scanned = SecurityReportFactory(
            project=project,
            matched_cve_id="CVE-2024-2",
            status="valid",
            hardening_scanned_at=timezone.now(),
        )
        SecurityReportFactory(
            project=project, matched_cve_id="CVE-2024-3", status="closed"
        )  # closed
        ids = set(cve_report_candidates().values_list("pk", flat=True))
        assert hit.pk in ids
        assert scanned.pk not in ids


@pytest.mark.django_db
class TestMineReport:
    def test_creates_ideas_and_stamps_report(self) -> None:
        report = SecurityReportFactory(matched_cve_id="CVE-2024-9", status="valid")
        output = _idea_json(
            {
                "title": "Validate empty input",
                "description": "Reject empty buffers earlier.",
                "category": "validation",
                "severity": "minor",
                "file_path": "core/x.py",
                "still_exists": "yes",
                "still_exists_evidence": "still no guard",
                "draft_ticket_title": "Validate empty input",
                "draft_ticket_body": "Reject empty buffers earlier.",
            }
        )
        fake = _executor_with(output)
        with (
            patch(
                "franktheunicorn.review.tool_executor.make_executor",
                return_value=fake,
            ),
            patch(
                "franktheunicorn.security.hardening_ideas.resolve_verifier_reviewer",
                return_value=AgentCLIReviewerConfig(name="claude", cli_path="claude"),
            ),
        ):
            run = mine_report(report, _operator())
        assert run.error == ""
        assert run.ideas_created == 1
        report.refresh_from_db()
        assert report.hardening_scanned_at is not None
        assert report.hardening_idea_count == 1
        idea = HardeningIdea.objects.get()
        assert idea.title == "Validate empty input"
        assert idea.still_exists == "yes"
        assert idea.status == HardeningIdea.STATUS_CONFIRMED

    def test_skips_gone_and_dedups(self) -> None:
        report = SecurityReportFactory(matched_cve_id="CVE-2024-9", status="valid")
        HardeningIdea.objects.create(
            project=report.project,
            source_report=report,
            fingerprint=idea_fingerprint(report.project_id, "Already filed", "a.py"),
            title="Already filed",
            file_path="a.py",
            still_exists="yes",
        )
        output = _idea_json(
            {
                "title": "Already filed",
                "description": "dup",
                "file_path": "a.py",
                "still_exists": "yes",
            },
            {
                "title": "Gone already",
                "description": "fixed",
                "file_path": "b.py",
                "still_exists": "no",
            },
        )
        fake = _executor_with(output)
        with (
            patch(
                "franktheunicorn.review.tool_executor.make_executor",
                return_value=fake,
            ),
            patch(
                "franktheunicorn.security.hardening_ideas.resolve_verifier_reviewer",
                return_value=AgentCLIReviewerConfig(name="claude", cli_path="claude"),
            ),
        ):
            run = mine_report(report, _operator())
        assert run.ideas_created == 0
        assert run.ideas_skipped_dup == 1
        assert run.ideas_skipped_gone == 1
        report.refresh_from_db()
        assert report.hardening_scanned_at is not None

    def test_already_scanned_is_a_no_op(self) -> None:
        report = SecurityReportFactory(
            matched_cve_id="CVE-2024-9",
            hardening_scanned_at=timezone.now(),
        )
        run = mine_report(report, _operator())
        assert "already mined" in run.error

    def test_agent_failure_does_not_stamp(self) -> None:
        report = SecurityReportFactory(matched_cve_id="CVE-2024-9", status="valid")
        fake = _FakeExecutor(
            {
                "fetch": ExecResult(returncode=0, stdout="", stderr=""),
                "symbolic-ref": ExecResult(returncode=0, stdout="origin/master\n", stderr=""),
                "checkout": ExecResult(returncode=0, stdout="", stderr=""),
                "clean": ExecResult(returncode=0, stdout="", stderr=""),
                "rev-parse HEAD": ExecResult(returncode=0, stdout="deadbeefcafe\n", stderr=""),
                "claude": ExecResult(returncode=1, stdout="", stderr="boom"),
            }
        )
        with (
            patch(
                "franktheunicorn.review.tool_executor.make_executor",
                return_value=fake,
            ),
            patch(
                "franktheunicorn.security.hardening_ideas.resolve_verifier_reviewer",
                return_value=AgentCLIReviewerConfig(name="claude", cli_path="claude"),
            ),
        ):
            run = mine_report(report, _operator())
        assert run.error
        assert not run.stamped
        report.refresh_from_db()
        assert report.hardening_scanned_at is None


@pytest.mark.django_db
class TestMineBacklog:
    def test_respects_cap(self) -> None:
        project = ProjectFactory()
        for i in range(3):
            SecurityReportFactory(project=project, matched_cve_id=f"CVE-2024-{i}", status="valid")
        fake = _executor_with(_idea_json())
        op = _operator(hardening=_hardening(max_reports_per_run=2))
        with (
            patch(
                "franktheunicorn.review.tool_executor.make_executor",
                return_value=fake,
            ),
            patch(
                "franktheunicorn.security.hardening_ideas.resolve_verifier_reviewer",
                return_value=AgentCLIReviewerConfig(name="claude", cli_path="claude"),
            ),
        ):
            result = mine_backlog(op)
        assert result.reports_examined == 2
        assert SecurityReport.objects.filter(hardening_scanned_at__isnull=False).count() == 2
