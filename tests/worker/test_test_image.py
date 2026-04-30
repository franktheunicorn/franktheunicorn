"""Tests for the test runner image resolver / auto-builder."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from franktheunicorn.config.models import TestAutoBuildConfig, TestExecutionConfig
from franktheunicorn.worker.test_image import (
    DEFAULT_IMAGE,
    generate_auto_build_dockerfile,
    resolve_image,
)


def _docker_with_no_cached_image() -> MagicMock:
    docker = MagicMock()
    docker.images.get.side_effect = Exception("not found")
    return docker


def test_default_when_nothing_set(tmp_path: Path) -> None:
    docker = _docker_with_no_cached_image()
    cfg = TestExecutionConfig(enabled=True)
    assert resolve_image(docker, "owner", "repo", cfg, tmp_path) == DEFAULT_IMAGE
    docker.images.build.assert_not_called()


def test_prebuilt_image_used_as_is(tmp_path: Path) -> None:
    docker = _docker_with_no_cached_image()
    cfg = TestExecutionConfig(enabled=True, container_image="ghcr.io/x/y:latest")
    assert resolve_image(docker, "owner", "repo", cfg, tmp_path) == "ghcr.io/x/y:latest"
    docker.images.build.assert_not_called()


def test_dockerfile_build_path(tmp_path: Path) -> None:
    (tmp_path / ".frank").mkdir()
    df = tmp_path / ".frank" / "Dockerfile"
    df.write_text("FROM python:3.12-slim\n")

    docker = _docker_with_no_cached_image()
    cfg = TestExecutionConfig(enabled=True, dockerfile=".frank/Dockerfile")
    tag = resolve_image(docker, "owner", "repo", cfg, tmp_path)

    assert tag.startswith("franktheunicorn-test/owner-repo:")
    docker.images.build.assert_called_once()
    kwargs = docker.images.build.call_args.kwargs
    assert kwargs["dockerfile"] == ".frank/Dockerfile"
    assert kwargs["path"] == str(tmp_path)
    assert kwargs["tag"] == tag


def test_dockerfile_missing_raises(tmp_path: Path) -> None:
    docker = _docker_with_no_cached_image()
    cfg = TestExecutionConfig(enabled=True, dockerfile="missing/Dockerfile")
    with pytest.raises(FileNotFoundError):
        resolve_image(docker, "owner", "repo", cfg, tmp_path)


def test_dockerfile_always_invokes_build(tmp_path: Path) -> None:
    """Mode B always calls docker build (BuildKit handles the layer cache).

    A tag-level skip would silently reuse a stale image when files COPYed by
    the Dockerfile (requirements.txt, etc.) change without the Dockerfile
    itself being edited — producing incorrect verdicts.
    """
    df = tmp_path / "Dockerfile"
    df.write_text("FROM scratch\n")

    docker = MagicMock()
    docker.images.get.return_value = MagicMock()  # would-be cache hit
    cfg = TestExecutionConfig(enabled=True, dockerfile="Dockerfile")
    tag = resolve_image(docker, "o", "r", cfg, tmp_path)
    assert tag.startswith("franktheunicorn-test/o-r:")
    docker.images.build.assert_called_once()


def test_auto_build_generates_and_builds(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n")

    docker = _docker_with_no_cached_image()
    cfg = TestExecutionConfig(
        enabled=True,
        auto_build=TestAutoBuildConfig(
            base_image="python:3.12-slim",
            requirements_files=["requirements.txt"],
            setup_commands=["pip install -e ."],
        ),
    )
    tag = resolve_image(docker, "owner", "repo", cfg, tmp_path)

    assert tag.startswith("franktheunicorn-test/owner-repo:")
    docker.images.build.assert_called_once()
    # Generated Dockerfile is removed after the build.
    assert not list(tmp_path.glob(".frank-autobuild-*.Dockerfile"))


def test_generate_auto_build_dockerfile_shape() -> None:
    cfg = TestAutoBuildConfig(
        base_image="python:3.12-slim",
        requirements_files=["requirements.txt", "requirements-test.txt"],
        setup_commands=["pip install -e .", "echo done"],
    )
    df = generate_auto_build_dockerfile(cfg, "/workspace")
    assert "FROM python:3.12-slim" in df
    assert "WORKDIR /workspace" in df
    assert "COPY requirements.txt requirements-test.txt /workspace/" in df
    assert "-r requirements.txt -r requirements-test.txt" in df
    assert "RUN pip install -e ." in df
    assert "RUN echo done" in df


def test_auto_build_hash_changes_with_requirements(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest==8.0\n")
    docker = _docker_with_no_cached_image()
    cfg = TestExecutionConfig(
        enabled=True,
        auto_build=TestAutoBuildConfig(requirements_files=["requirements.txt"]),
    )
    tag1 = resolve_image(docker, "o", "r", cfg, tmp_path)

    (tmp_path / "requirements.txt").write_text("pytest==9.0\n")
    tag2 = resolve_image(docker, "o", "r", cfg, tmp_path)
    assert tag1 != tag2
