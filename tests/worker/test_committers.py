"""Boot-time committer sync: write access to committer_repos, stored on the row."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.models import Project
from franktheunicorn.worker.committers import sync_committers_at_boot, sync_project_committers
from tests.factories import ProjectFactory


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.github.test/x")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(code, request=request)
    )


class _Client:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, str]] = []

    def list_team_members(self, org: str, team_slug: str) -> list[str]:
        self.calls.append((org, team_slug, "team"))
        answer = self.answers[f"team:{org}/{team_slug}"]
        if isinstance(answer, Exception):
            raise answer
        return list(answer)

    def list_repo_collaborators(self, owner: str, repo: str, permission: str = "push") -> list[str]:
        self.calls.append((owner, repo, permission))
        answer = self.answers[f"{owner}/{repo}"]
        if isinstance(answer, Exception):
            raise answer
        return list(answer)


def _pc(**kwargs: Any) -> ProjectConfig:
    return ProjectConfig(owner="apache", repo="spark", **kwargs)


@pytest.mark.django_db
class TestSyncProjectCommitters:
    def test_unions_repos_and_drops_bots(self) -> None:
        client = _Client(
            {
                "apache/spark": ["HyukjinKwon", "asfgit[bot]", "cloud-fan"],
                "apache/spark-private": ["hyukjinkwon", "holdenk"],
            }
        )
        pc = _pc(committer_repos=["apache/spark", "apache/spark-private"])
        result = sync_project_committers(client, pc)  # type: ignore[arg-type]
        assert result.stored
        assert client.calls[0] == ("apache", "spark", "push")
        row = Project.objects.get(owner="apache", repo="spark")
        assert row.committers_cache == ["cloud-fan", "holdenk", "HyukjinKwon"]
        assert row.committers_cache_at is not None

    def test_one_unreadable_repo_still_stores_the_rest(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _Client({"apache/spark": ["holdenk"], "apache/spark-private": _status_error(404)})
        pc = _pc(committer_repos=["apache/spark-private", "apache/spark"])
        with caplog.at_level(logging.WARNING):
            result = sync_project_committers(client, pc)  # type: ignore[arg-type]
        assert result.stored and result.read == ["apache/spark"]
        assert "cannot see it" in caplog.text and "using apache/spark" in caplog.text
        assert Project.objects.get(repo="spark").committers_cache == ["holdenk"]

    def test_nothing_readable_keeps_the_stored_list(self, caplog: pytest.LogCaptureFixture) -> None:
        ProjectFactory(owner="apache", repo="spark", committers_cache=["holdenk"])
        client = _Client({"apache/spark": _status_error(403)})
        with caplog.at_level(logging.WARNING):
            result = sync_project_committers(client, _pc(committer_repos=["apache/spark"]))  # type: ignore[arg-type]
        assert not result.stored
        assert "needs a token with write access" in caplog.text
        assert "keeping the committers stored" in caplog.text
        assert Project.objects.get(repo="spark").committers_cache == ["holdenk"]

    def test_team_first_and_repos_unread_when_it_answers(self) -> None:
        client = _Client({"team:apache/spark-committers": ["holdenk"], "apache/spark": ["infra"]})
        pc = _pc(committer_teams=["apache/spark-committers"], committer_repos=["apache/spark"])
        result = sync_project_committers(client, pc)  # type: ignore[arg-type]
        assert result.read == ["apache/spark-committers"]
        assert client.calls == [("apache", "spark-committers", "team")]
        assert Project.objects.get(repo="spark").committers_cache == ["holdenk"]

    def test_repos_are_the_fallback_when_no_team_answers(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _Client(
            {"team:apache/spark-committers": _status_error(404), "apache/spark": ["holdenk"]}
        )
        pc = _pc(committer_teams=["apache/spark-committers"], committer_repos=["apache/spark"])
        with caplog.at_level(logging.WARNING):
            result = sync_project_committers(client, pc)  # type: ignore[arg-type]
        assert result.read == ["apache/spark"]
        assert "not visible to this token" in caplog.text and "using apache/spark" in caplog.text
        assert Project.objects.get(repo="spark").committers_cache == ["holdenk"]

    @pytest.mark.parametrize(
        ("error", "text"),
        [
            (_status_error(401), "token was refused"),
            (_status_error(500), "HTTP 500"),
            (ValueError("bad json"), "ValueError: bad json"),
            (NotImplementedError("GiteaClient cannot list"), "GiteaClient cannot list"),
        ],
    )
    def test_failure_reasons(
        self, error: Exception, text: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _Client({"apache/spark": error})
        with caplog.at_level(logging.WARNING):
            sync_project_committers(client, _pc(committer_repos=["apache/spark"]))  # type: ignore[arg-type]
        assert text in caplog.text


@pytest.mark.django_db
class TestSyncAtBoot:
    def test_only_enabled_projects_that_name_repos(self, caplog: pytest.LogCaptureFixture) -> None:
        client = _Client({"apache/spark": ["holdenk"], "team:apache/t": ["bob"]})
        configs = [
            _pc(committer_repos=["apache/spark"]),
            ProjectConfig(
                owner="apache", repo="off", enabled=False, committer_repos=["apache/spark"]
            ),
            ProjectConfig(owner="apache", repo="none"),
            ProjectConfig(owner="apache", repo="teams", committer_teams=["apache/t"]),
            ProjectConfig(owner="x", repo="y", forge="gitlab", committer_repos=["x/y"]),
            "not a config",
        ]
        with caplog.at_level(logging.WARNING):
            results = sync_committers_at_boot({"github": client}, configs)  # type: ignore[dict-item]
        assert [r.project for r in results] == ["apache/spark", "apache/teams"]
        assert len(client.calls) == 2
        assert "no client for forge 'gitlab'" in caplog.text

    def test_a_crash_does_not_stop_boot(self, caplog: pytest.LogCaptureFixture) -> None:
        class _Broken:
            def list_repo_collaborators(self, *a: object, **k: object) -> list[str]:
                return [None]  # type: ignore[list-item]

        with caplog.at_level(logging.ERROR):
            results = sync_committers_at_boot(
                {"github": _Broken()},  # type: ignore[dict-item]
                [_pc(committer_repos=["apache/spark"])],
            )
        assert results == []
        assert "committer sync failed" in caplog.text


def test_mock_client_reads_fixtures(tmp_path: Any) -> None:
    import json

    from franktheunicorn.backends.mock import MockForgeClient

    (tmp_path / "apache_spark_collaborators.json").write_text(json.dumps([{"login": "a"}, {}]))
    (tmp_path / "apache_team_spark-committers_members.json").write_text(
        json.dumps([{"login": "b"}])
    )
    client = MockForgeClient(tmp_path)
    assert client.list_repo_collaborators("apache", "spark") == ["a"]
    assert client.list_team_members("apache", "spark-committers") == ["b"]
    assert client.list_repo_collaborators("apache", "other") == []
    assert client.list_team_members("apache", "other") == []


def test_forges_without_the_calls_say_so() -> None:
    from franktheunicorn.backends.gitea import GiteaClient

    client = GiteaClient(token="t", base_url="https://gitea.test")
    with pytest.raises(NotImplementedError):
        client.list_repo_collaborators("a", "b")
    with pytest.raises(NotImplementedError):
        client.list_team_members("a", "b")


class TestCommitterClients:
    def _oc(self, **kwargs: Any) -> Any:
        from franktheunicorn.config.models import OperatorConfig

        return OperatorConfig(github_token="poll", github_username="holdenk", **kwargs)

    def test_no_committers_token_keeps_the_polling_client(self) -> None:
        from franktheunicorn.worker.committers import committer_clients

        polling = object()
        out = committer_clients(self._oc(), {"github": polling})  # type: ignore[dict-item]
        assert out["github"] is polling

    def test_committers_token_gets_its_own_unlimited_client(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from franktheunicorn.backends.github import GitHubClient
        from franktheunicorn.worker.committers import committer_clients

        polling = object()
        with caplog.at_level(logging.INFO):
            out = committer_clients(
                self._oc(github_committers_token="live"),
                {"github": polling, "elsewhere": polling},  # type: ignore[dict-item]
            )
        client = out["github"]
        assert isinstance(client, GitHubClient)
        assert client is not polling
        assert out["elsewhere"] is polling  # no forge entry by that name
        assert "reading committers with committers_token" in caplog.text

    def test_same_token_is_not_rebuilt(self) -> None:
        from franktheunicorn.worker.committers import committer_clients

        polling = object()
        out = committer_clients(
            self._oc(github_committers_token="poll"),
            {"github": polling},  # type: ignore[dict-item]
        )
        assert out["github"] is polling

    def test_non_github_forge_goes_through_make_client(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from franktheunicorn.config.models import ForgeRegistryEntry, OperatorConfig
        from franktheunicorn.worker.committers import committer_clients

        def entry(name: str) -> ForgeRegistryEntry:
            return ForgeRegistryEntry(
                name=name,
                type="gitlab",
                base_url="https://gl.test",
                token="p",
                committers_token="c",
            )

        oc = OperatorConfig(forges=[entry("gl"), entry("bad")])
        polling = object()
        out = committer_clients(oc, {"gl": polling})  # type: ignore[dict-item]
        assert out["gl"] is not polling

        def refuse(*a: object, **k: object) -> None:
            raise NotImplementedError("nope")

        monkeypatch.setattr("franktheunicorn.backends.make_client", refuse)
        with caplog.at_level(logging.WARNING):
            out = committer_clients(oc, {"bad": polling})  # type: ignore[dict-item]
        assert out["bad"] is polling
        assert "could not build a client on committers_token" in caplog.text
