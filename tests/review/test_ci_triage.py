"""LLM triage of a PR's failed CI checks."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from pytest_httpx import HTTPXMock

from franktheunicorn.backends.base import FailedCheck
from franktheunicorn.backends.github import GitHubClient
from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig
from franktheunicorn.core.models import CIFailureAnalysis, PullRequest, WorkerCommand
from franktheunicorn.review.backends.base import BaseLLMBackend
from franktheunicorn.review.ci_triage import analyze_ci_failures, excerpt_log, parse_verdict

API = "https://api.github.test"
SHA = "c" * 40


def _check(i: int, name: str = "Run / Build modules: sql") -> FailedCheck:
    return FailedCheck(
        repo="someone/spark", id=i, name=name, conclusion="failure", url="u", kind="actions"
    )


def test_excerpt_keeps_the_error_and_the_tail_not_the_noise() -> None:
    log = "\n".join(
        [f"2026-10-14T10:00:00.0000000Z setup step {i}" for i in range(500)]
        + ["2026-10-14T10:00:01.0Z [error] SQLQuerySuite: *** FAILED ***"]
        + [f"build line {i}" for i in range(500)]
    )
    out = excerpt_log(log)
    assert "[error] SQLQuerySuite: *** FAILED ***" in out
    assert "build line 499" in out  # tail
    assert "setup step 10\n" not in out
    assert "2026-10-14T" not in out


def test_parse_verdict_defaults_garbage_to_unclear() -> None:
    good = parse_verdict(
        'Sure: {"verdict": "flake", "confidence": 1.7, "summary": "s", "indicates": "i"}'
    )
    assert good["verdict"] == "flake"
    assert good["confidence"] == 1.0
    bad = parse_verdict("I think it's flaky?")
    assert bad["verdict"] == "unclear"
    assert bad["confidence"] is None


@pytest.mark.django_db
def test_each_failed_check_is_analysed_once(db_pr: PullRequest) -> None:
    db_pr.head_sha = SHA
    db_pr.save()
    forge = MagicMock()
    forge.list_failed_checks.return_value = [_check(1), _check(2, "Run / Linters")]
    forge.get_check_log.return_value = "[error] boom"
    backend = MagicMock(spec=BaseLLMBackend)
    backend.metered_call.return_value = '{"verdict": "pr", "summary": "lint", "confidence": 0.9}'
    op = OperatorConfig(llm_backends=[LLMBackendConfig(provider="openai", model="m")])

    with patch("franktheunicorn.review.backends.get_backend", return_value=backend):
        assert analyze_ci_failures(db_pr, forge, op) == 2
        assert analyze_ci_failures(db_pr, forge, op) == 0

    assert backend.metered_call.call_count == 2
    row = CIFailureAnalysis.objects.get(pull_request=db_pr, check_id=2)
    assert (row.verdict, row.confidence, row.head_sha) == ("pr", 0.9, SHA)
    assert "Run / Linters" in backend.metered_call.call_args.args[1]


@pytest.mark.django_db
def test_a_status_and_a_check_run_sharing_an_id_are_both_analysed(db_pr: PullRequest) -> None:
    forge = MagicMock()
    status = FailedCheck(
        repo="someone/spark", id=1, name="jenkins", conclusion="error", url="", kind="status"
    )
    forge.list_failed_checks.return_value = [_check(1), status]
    forge.get_check_log.return_value = ""
    backend = MagicMock(spec=BaseLLMBackend)
    backend.metered_call.return_value = '{"verdict": "infra"}'
    op = OperatorConfig(llm_backends=[LLMBackendConfig(provider="openai", model="m")])
    with patch("franktheunicorn.review.backends.get_backend", return_value=backend):
        assert analyze_ci_failures(db_pr, forge, op) == 2
    assert db_pr.ci_failure_analyses.count() == 2


@pytest.mark.django_db
def test_no_usable_backend_is_an_error_not_a_clean_ci(db_pr: PullRequest) -> None:
    forge = MagicMock()
    forge.list_failed_checks.return_value = [_check(1)]
    op = OperatorConfig(llm_backends=[LLMBackendConfig(provider="openai", model="dead")])
    with (
        patch("franktheunicorn.review.backends.get_backend") as get_backend,
        pytest.raises(RuntimeError, match="no usable llm_backends"),
    ):
        analyze_ci_failures(db_pr, forge, op, frozenset({0}))
    get_backend.assert_not_called()


def test_github_reads_failures_from_the_fork_too(httpx_mock: HTTPXMock) -> None:
    """Spark's jobs run on the contributor's fork; apache/spark only gets "Build"."""
    httpx_mock.add_response(
        url=f"{API}/repos/apache/spark/pulls/7", json={"head": {"repo": {"full_name": "me/spark"}}}
    )

    def runs(*items: tuple[int, str, str]) -> dict[str, Any]:
        return {
            "check_runs": [
                {
                    "id": i,
                    "name": n,
                    "conclusion": c,
                    "html_url": "h",
                    "app": {"slug": "github-actions"},
                }
                for i, n, c in items
            ]
        }

    httpx_mock.add_response(
        url=f"{API}/repos/apache/spark/commits/{SHA}/check-runs?per_page=100&page=1",
        json=runs((1, "Build", "success")),
    )
    httpx_mock.add_response(
        url=f"{API}/repos/apache/spark/commits/{SHA}/status?per_page=100", json={"statuses": []}
    )
    httpx_mock.add_response(
        url=f"{API}/repos/me/spark/commits/{SHA}/check-runs?per_page=100&page=1",
        json=runs((2, "Run / sql", "failure"), (3, "Run / old", "cancelled")),
    )
    httpx_mock.add_response(
        url=f"{API}/repos/me/spark/commits/{SHA}/status?per_page=100",
        json={"statuses": [{"id": 9, "context": "jenkins", "state": "error", "target_url": "j"}]},
    )
    client = GitHubClient(token="t", base_url=API)
    failed = client.list_failed_checks("apache", "spark", 7, SHA)
    assert [(c.repo, c.id, c.kind) for c in failed] == [
        ("me/spark", 2, "actions"),
        ("me/spark", 9, "status"),
    ]

    httpx_mock.add_response(url=f"{API}/repos/me/spark/actions/jobs/2/logs", text="the log")
    assert client.get_check_log(failed[0]) == "the log"
    assert client.get_check_log(failed[1]) == ""
    client.close()


@pytest.mark.django_db
def test_viewing_a_pr_queues_triage_once_and_renders_it(client: Client, db_pr: PullRequest) -> None:
    db_pr.head_sha = SHA
    db_pr.state = "open"
    db_pr.save()
    with patch("franktheunicorn.config.loader.get_project_config", return_value=None):
        client.get(f"/pr/{db_pr.pk}/")
        client.get(f"/pr/{db_pr.pk}/")
    assert WorkerCommand.objects.filter(command="analyze_ci_failures").count() == 1

    CIFailureAnalysis.objects.create(
        pull_request=db_pr,
        head_sha=SHA,
        check_repo="me/spark",
        check_id=1,
        check_name="Run / sql",
        conclusion="failure",
        verdict="flake",
        summary="KafkaSuite timed out",
    )
    WorkerCommand.objects.update(status="completed")
    fragment = client.get(f"/pr/{db_pr.pk}/ci-failures/").content.decode()
    assert "likely flake" in fragment
    assert "KafkaSuite timed out" in fragment
    assert "every 5s" not in fragment


@pytest.mark.django_db
def test_clean_ci_is_only_claimed_for_the_head_it_ran_on(
    client: Client, db_pr: PullRequest
) -> None:
    db_pr.head_sha = SHA
    db_pr.save()
    WorkerCommand.objects.create(
        command="analyze_ci_failures",
        pull_request=db_pr,
        status="completed",
        log=f"Analysed 0 failed CI check(s) on {'d' * 12}",
    )
    url = f"/pr/{db_pr.pk}/ci-failures/"
    assert "No failed CI checks" not in client.get(url).content.decode()
    WorkerCommand.objects.update(log=f"Analysed 0 failed CI check(s) on {SHA[:12]}")
    assert "No failed CI checks" in client.get(url).content.decode()


@pytest.mark.django_db
def test_a_forks_javascript_status_url_is_not_stored(db_pr: PullRequest) -> None:
    """A fork's commit status sets its own target_url, and the dashboard links it."""
    forge = MagicMock()
    forge.list_failed_checks.return_value = [
        FailedCheck(
            repo="evil/spark",
            id=1,
            name="ci",
            conclusion="error",
            url="javascript:x()",
            kind="status",
        )
    ]
    forge.get_check_log.return_value = ""
    backend = MagicMock(spec=BaseLLMBackend)
    backend.metered_call.return_value = "{}"
    op = OperatorConfig(llm_backends=[LLMBackendConfig(provider="openai", model="m")])
    with patch("franktheunicorn.review.backends.get_backend", return_value=backend):
        analyze_ci_failures(db_pr, forge, op)
    assert db_pr.ci_failure_analyses.get().check_url == ""
