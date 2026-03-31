"""Tests for management commands."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command


@pytest.mark.django_db
class TestSendDigestCommand:
    def test_runs_without_error(self) -> None:
        out = StringIO()
        call_command("send_digest", stdout=out)
        output = out.getvalue()
        assert "franktheunicorn digest" in output
        assert "not sent" in output.lower() or "sent" in output.lower()


class TestAddProjectCommand:
    def test_creates_yaml_file(self, tmp_path: Path) -> None:
        out = StringIO()
        call_command(
            "add_project",
            "--repo=testorg/testrepo",
            f"--output-dir={tmp_path}",
            stdout=out,
        )
        output = out.getvalue()
        assert "Created" in output
        yaml_file = tmp_path / "testorg-testrepo.yaml"
        assert yaml_file.exists()
        content = yaml_file.read_text()
        assert "testorg" in content
        assert "testrepo" in content

    def test_invalid_repo_format(self, tmp_path: Path) -> None:
        err = StringIO()
        call_command(
            "add_project",
            "--repo=invalid",
            f"--output-dir={tmp_path}",
            stderr=err,
        )
        assert "owner/repo" in err.getvalue()


@pytest.mark.django_db
class TestDetectCollaboratorsCommand:
    def test_runs_dry_run(self) -> None:
        out = StringIO()
        call_command(
            "detect_collaborators",
            "--project=apache/spark",
            "--dry-run",
            stdout=out,
        )
        output = out.getvalue()
        assert "collaborators" in output.lower()
