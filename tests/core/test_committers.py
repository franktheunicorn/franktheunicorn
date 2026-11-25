"""core.committers: the config's hand list unioned with the boot-time read."""

from __future__ import annotations

from types import SimpleNamespace

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.core.committers import committers_for_pr, project_committers


def test_union_case_insensitive_config_first() -> None:
    pc = ProjectConfig(owner="apache", repo="spark", committers=["HoldenK", " "])
    project = SimpleNamespace(committers_cache=["holdenk", "cloud-fan"])
    assert project_committers(pc, project) == ["HoldenK", "cloud-fan"]


def test_missing_sources() -> None:
    assert project_committers(None, None) == []
    assert project_committers(ProjectConfig(owner="a", repo="b"), SimpleNamespace()) == []
    assert project_committers(SimpleNamespace(committers="nope"), None) == []


class _NoProject:
    @property
    def project(self) -> object:
        raise RuntimeError("database access not allowed")


def test_committers_for_pr_survives_an_unloadable_project() -> None:
    pc = ProjectConfig(owner="a", repo="b", committers=["bob"])
    assert committers_for_pr(pc, _NoProject()) == ["bob"]
    pr = SimpleNamespace(project=SimpleNamespace(committers_cache=["carol"]))
    assert committers_for_pr(pc, pr) == ["bob", "carol"]


def test_committer_repos_validation() -> None:
    import pytest
    from pydantic import ValidationError

    pc = ProjectConfig(owner="a", repo="b", committer_repos=[" apache/spark/ ", ""])
    assert pc.committer_repos == ["apache/spark"]
    for bad in (["spark"], ["apache/"], ["a/b/c"]):
        with pytest.raises(ValidationError):
            ProjectConfig(owner="a", repo="b", committer_repos=bad)
    teams = ProjectConfig(
        owner="a",
        repo="b",
        committer_teams=[
            "https://github.com/orgs/apache/teams/spark-committers",
            "apache/spark-pmc/",
        ],
    )
    assert teams.committer_teams == ["apache/spark-committers", "apache/spark-pmc"]
    with pytest.raises(ValidationError, match="org/team-slug"):
        ProjectConfig(owner="a", repo="b", committer_teams=["spark-committers"])
    # A team URL is not a repo.
    with pytest.raises(ValidationError):
        ProjectConfig(
            owner="a", repo="b", committer_repos=["https://github.com/orgs/apache/teams/x"]
        )
