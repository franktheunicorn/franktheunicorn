"""Tests for the operator-level forge registry and project forge field."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from franktheunicorn.backends import make_client
from franktheunicorn.backends.gitea import GiteaClient
from franktheunicorn.backends.github import GitHubClient
from franktheunicorn.backends.gitlab import GitLabClient
from franktheunicorn.config.models import ForgeRegistryEntry, OperatorConfig, ProjectConfig
from franktheunicorn.config.resolver import get_forge_entry


class TestForgeRegistryEntry:
    def test_github_default_base_url(self) -> None:
        entry = ForgeRegistryEntry(name="github", type="github", token="t")
        assert entry.base_url == "https://api.github.com"

    def test_forgejo_defaults_to_codeberg(self) -> None:
        entry = ForgeRegistryEntry(name="codeberg", type="forgejo", token="t")
        assert entry.base_url == "https://codeberg.org"

    def test_gitlab_defaults_to_gitlab_com(self) -> None:
        entry = ForgeRegistryEntry(name="gl", type="gitlab", token="t")
        assert entry.base_url == "https://gitlab.com"

    def test_gitea_requires_explicit_base_url(self) -> None:
        with pytest.raises(ValidationError, match="requires base_url"):
            ForgeRegistryEntry(name="gitea", type="gitea", token="t")

    def test_explicit_base_url_overrides_default(self) -> None:
        entry = ForgeRegistryEntry(
            name="ghe", type="github", base_url="https://ghe.example.com/api/v3", token="t"
        )
        assert entry.base_url == "https://ghe.example.com/api/v3"

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown forge type"):
            ForgeRegistryEntry(name="weird", type="bitbucket", base_url="https://x", token="t")

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            ForgeRegistryEntry(name="  ", type="github", token="t")

    def test_self_hosted_gitea(self) -> None:
        entry = ForgeRegistryEntry(
            name="work", type="gitea", base_url="https://git.work.example", token="t"
        )
        assert entry.base_url == "https://git.work.example"


class TestSynthesizeDefaultForge:
    def test_legacy_github_token_creates_github_entry(self) -> None:
        oc = OperatorConfig(github_token="ghp_abc123", github_username="alice")
        assert len(oc.forges) == 1
        entry = oc.forges[0]
        assert entry.name == "github"
        assert entry.type == "github"
        assert entry.token == "ghp_abc123"
        assert entry.username == "alice"
        assert entry.base_url == "https://api.github.com"

    def test_no_synthesis_when_token_empty(self) -> None:
        oc = OperatorConfig()
        assert oc.forges == []

    def test_no_synthesis_when_forges_already_present(self) -> None:
        oc = OperatorConfig(
            github_token="ghp_abc",
            forges=[
                ForgeRegistryEntry(
                    name="codeberg", type="forgejo", token="t1", base_url="https://codeberg.org"
                )
            ],
        )
        assert len(oc.forges) == 1
        assert oc.forges[0].name == "codeberg"

    def test_multiple_explicit_forges(self) -> None:
        oc = OperatorConfig(
            forges=[
                ForgeRegistryEntry(name="github", type="github", token="g"),
                ForgeRegistryEntry(name="codeberg", type="forgejo", token="c"),
                ForgeRegistryEntry(name="gl", type="gitlab", token="l"),
            ]
        )
        assert [e.name for e in oc.forges] == ["github", "codeberg", "gl"]


class TestForgeTokensSet:
    def test_empty_token_rejected(self) -> None:
        with pytest.raises(ValidationError, match="empty token"):
            OperatorConfig(
                forges=[
                    ForgeRegistryEntry(name="codeberg", type="forgejo", token=""),
                ]
            )

    def test_error_lists_offending_entry_names(self) -> None:
        with pytest.raises(ValidationError, match="codeberg"):
            OperatorConfig(
                forges=[
                    ForgeRegistryEntry(name="github", type="github", token="g"),
                    ForgeRegistryEntry(name="codeberg", type="forgejo", token=""),
                ]
            )

    def test_mock_mode_bypasses_check(self) -> None:
        oc = OperatorConfig(
            mock_mode=True,
            forges=[ForgeRegistryEntry(name="codeberg", type="forgejo", token="")],
        )
        assert oc.forges[0].token == ""

    def test_no_forges_no_error(self) -> None:
        OperatorConfig()  # empty registry is fine

    def test_synthesized_default_with_real_token_passes(self) -> None:
        oc = OperatorConfig(github_token="ghp_real")
        assert oc.forges[0].token == "ghp_real"


class TestForgeNamesUnique:
    def test_duplicate_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate forge name"):
            OperatorConfig(
                forges=[
                    ForgeRegistryEntry(name="primary", type="github", token="a"),
                    ForgeRegistryEntry(name="primary", type="forgejo", token="b"),
                ]
            )


class TestProjectForgeField:
    def test_default_is_github(self) -> None:
        pc = ProjectConfig(owner="acme", repo="widget")
        assert pc.forge == "github"

    def test_custom_forge_name(self) -> None:
        pc = ProjectConfig(owner="acme", repo="widget", forge="codeberg")
        assert pc.forge == "codeberg"


class TestGetForgeEntry:
    def test_returns_named_entry(self) -> None:
        oc = OperatorConfig(
            forges=[
                ForgeRegistryEntry(name="github", type="github", token="g"),
                ForgeRegistryEntry(name="codeberg", type="forgejo", token="c"),
            ]
        )
        assert get_forge_entry(oc, "codeberg").type == "forgejo"

    def test_missing_name_raises(self) -> None:
        oc = OperatorConfig(github_token="g")  # synthesizes github entry
        with pytest.raises(KeyError, match="not found"):
            get_forge_entry(oc, "nonexistent")

    def test_empty_registry_error_lists_no_options(self) -> None:
        oc = OperatorConfig()
        with pytest.raises(KeyError, match="empty registry"):
            get_forge_entry(oc, "anything")


class TestMakeClientFactory:
    def test_github_returns_github_client(self) -> None:
        entry = ForgeRegistryEntry(name="gh", type="github", token="t")
        client = make_client(entry)
        try:
            assert isinstance(client, GitHubClient)
        finally:
            client.close()

    def test_forgejo_returns_gitea_client(self) -> None:
        entry = ForgeRegistryEntry(name="cb", type="forgejo", token="t")
        client = make_client(entry)
        try:
            assert isinstance(client, GiteaClient)
        finally:
            client.close()

    def test_gitea_returns_gitea_client(self) -> None:
        entry = ForgeRegistryEntry(
            name="work", type="gitea", base_url="https://git.work.example", token="t"
        )
        client = make_client(entry)
        try:
            assert isinstance(client, GiteaClient)
        finally:
            client.close()

    def test_gitlab_returns_gitlab_client(self) -> None:
        entry = ForgeRegistryEntry(name="gl", type="gitlab", token="t")
        client = make_client(entry)
        try:
            assert isinstance(client, GitLabClient)
        finally:
            client.close()
