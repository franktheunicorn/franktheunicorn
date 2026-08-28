"""Tests for the import_security_zip management command."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from franktheunicorn.core.models import SecurityReport, WorkerCommand
from tests.factories import ProjectFactory

# Must read as a security report — the importer filters on the parser's
# is_security_report verdict, same as the email door.
REPORT_TEXT = (
    "Path traversal vulnerability in the extractor lets a member name escape the\n"
    "target dir. The exploit allows arbitrary file write.\n"
)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    path = tmp_path / "reports.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.txt", REPORT_TEXT)
        zf.writestr("b.md", REPORT_TEXT + "A second, distinct exploit.")
        zf.writestr("logo.png", b"\x89PNG\r\n")
    return path


@pytest.fixture
def triage_on() -> Any:
    from franktheunicorn.config.models import (
        LLMBackendConfig,
        OperatorConfig,
        SecurityTriageConfig,
    )

    with patch("franktheunicorn.config.loader.get_operator_config") as mock:
        mock.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True, auto_triage=True),
            # Bulk queueing refuses without a backend, so this has to name one or
            # --triage correctly declines to queue anything.
            llm_backends=[LLMBackendConfig(provider="stub")],
        )
        yield mock


@pytest.mark.django_db
class TestImportSecurityZipCommand:
    def test_imports_reports_and_summarises(self, archive: Path, triage_on: Any) -> None:
        out = io.StringIO()

        call_command("import_security_zip", str(archive), stdout=out)

        assert SecurityReport.objects.count() == 2
        assert "2 imported" in out.getvalue()

    def test_reports_the_entries_it_passed_over(self, archive: Path, triage_on: Any) -> None:
        """A command that silently drops half an archive is worse than one that fails."""
        out = io.StringIO()

        call_command("import_security_zip", str(archive), stdout=out)

        assert "logo.png" in out.getvalue()
        assert "unsupported" in out.getvalue()

    def test_verbose_entries_lists_the_successes_too(self, archive: Path, triage_on: Any) -> None:
        out = io.StringIO()

        call_command("import_security_zip", str(archive), "--verbose-entries", stdout=out)

        assert "a.txt" in out.getvalue()
        assert "b.md" in out.getvalue()

    def test_project_flag_attaches_every_report(self, archive: Path, triage_on: Any) -> None:
        project = ProjectFactory(owner="apache", repo="spark")

        call_command(
            "import_security_zip", str(archive), "--project", "apache/spark", stdout=io.StringIO()
        )

        assert SecurityReport.objects.filter(project=project).count() == 2

    def test_does_not_triage_by_default(self, archive: Path, triage_on: Any) -> None:
        """Bulk is opt-in even with security_triage.auto_triage on — it's real money."""
        out = io.StringIO()

        call_command("import_security_zip", str(archive), stdout=out)

        assert SecurityReport.objects.count() == 2
        assert WorkerCommand.objects.count() == 0
        assert "--triage" in out.getvalue()

    def test_triage_flag_queues_every_report(self, archive: Path, triage_on: Any) -> None:
        call_command("import_security_zip", str(archive), "--triage", stdout=io.StringIO())

        assert WorkerCommand.objects.filter(command="run_security_triage").count() == 2

    def test_verify_versions_flag_queues_without_the_deep_verify_cap(
        self, archive: Path, triage_on: Any
    ) -> None:
        ProjectFactory(owner="apache", repo="spark")
        call_command(
            "import_security_zip",
            str(archive),
            "--project",
            "apache/spark",
            "--verify-versions",
            stdout=io.StringIO(),
        )
        assert WorkerCommand.objects.filter(command="map_report_versions").count() == 2
        assert WorkerCommand.objects.filter(command="verify_security_report").count() == 0

    def test_missing_file_is_a_command_error(self, tmp_path: Path) -> None:
        with pytest.raises(CommandError, match="No such file"):
            call_command("import_security_zip", str(tmp_path / "nope.zip"))

    def test_malformed_project_is_a_command_error(self, archive: Path) -> None:
        with pytest.raises(CommandError, match="owner/repo format"):
            call_command("import_security_zip", str(archive), "--project", "spark")

    def test_unknown_project_is_a_command_error(self, archive: Path) -> None:
        with pytest.raises(CommandError, match="No project"):
            call_command("import_security_zip", str(archive), "--project", "apache/nope")

    def test_a_bad_archive_is_a_command_error(self, tmp_path: Path) -> None:
        bogus = tmp_path / "reports.zip"
        bogus.write_bytes(b"not a zip at all")

        with pytest.raises(CommandError, match="not a valid zip archive"):
            call_command("import_security_zip", str(bogus))
