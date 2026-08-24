"""Tests for the security page's zip upload button and printed CLI command."""

from __future__ import annotations

import io
import zipfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from franktheunicorn.core.models import SecurityReport
from franktheunicorn.security import zip_import
from tests.factories import ProjectFactory

REPORT_TEXT = (
    "Path traversal vulnerability in the extractor: ../../etc/passwd escapes the\n"
    "target dir. The exploit allows arbitrary file write.\n"
)


def zip_bytes(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content.encode() if isinstance(content, str) else content)
    return buf.getvalue()


def upload(entries: dict[str, bytes | str], name: str = "reports.zip") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, zip_bytes(entries), content_type="application/zip")


@pytest.fixture
def triage_off() -> Any:
    """Auto-triage off, so these tests are about the view, not the worker queue."""
    from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig

    with patch("franktheunicorn.config.loader.get_operator_config") as mock:
        mock.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True, auto_triage=False),
        )
        yield mock


@pytest.mark.django_db
class TestSecurityListZipAffordances:
    def test_page_prints_the_manual_import_command(self, client: Client) -> None:
        response = client.get("/security/")

        assert response.status_code == 200
        body = response.content.decode()
        assert "manage.py import_security_zip" in body
        assert "--project owner/repo" in body
        assert "--triage" in body

    def test_page_offers_a_multipart_upload_form(self, client: Client) -> None:
        response = client.get("/security/")

        body = response.content.decode()
        assert 'action="/security/upload/"' in body
        assert 'enctype="multipart/form-data"' in body
        assert 'name="zip_file"' in body

    def test_project_options_come_from_enabled_projects(self, client: Client) -> None:
        enabled = ProjectFactory(enabled=True)
        disabled = ProjectFactory(enabled=False)

        body = client.get("/security/").content.decode()

        assert enabled.full_name in body
        assert disabled.full_name not in body

    def test_printed_command_names_the_running_interpreter(self) -> None:
        """Copy-pasteable, not a guess at where the operator installed things."""
        import sys

        from franktheunicorn.dashboard import views

        with patch.object(views, "_running_in_container", return_value=False):
            command = views._zip_import_command()

        assert command.endswith("manage.py import_security_zip reports.zip")
        # Either repo-relative (a venv under BASE_DIR) or the absolute path.
        assert sys.executable.endswith(command.split(" manage.py")[0].split("/")[-1])

    def test_under_docker_it_prints_a_command_the_host_shell_can_run(self) -> None:
        """The venv form is unrunnable in the shipped compose deployment.

        In the container this process is /usr/local/bin/python at /app, so the
        naive answer sends the operator to a host shell that has neither the
        interpreter nor the code nor the archive path.
        """
        from franktheunicorn.dashboard import views

        with patch.object(views, "_running_in_container", return_value=True):
            command = views._zip_import_command()

        assert command.startswith("docker compose exec")
        # data/ is the bind mount both containers share, so a file dropped there
        # from the host is a file this command can actually open.
        assert "data/reports.zip" in command

    def test_container_detection_uses_dockerenv(self) -> None:
        from franktheunicorn.dashboard.views import _running_in_container

        with patch("pathlib.Path.exists", return_value=True):
            assert _running_in_container() is True
        with patch("pathlib.Path.exists", return_value=False):
            assert _running_in_container() is False


@pytest.mark.django_db
class TestSecurityReportUpload:
    def test_imports_the_archive_and_reports_what_happened(
        self, client: Client, triage_off: Any
    ) -> None:
        response = client.post(
            "/security/upload/",
            {
                "zip_file": upload(
                    {"a.txt": REPORT_TEXT, "b.txt": REPORT_TEXT + " Second distinct exploit."}
                )
            },
            follow=True,
        )

        assert response.status_code == 200
        assert SecurityReport.objects.count() == 2
        assert b"2 imported" in response.content

    def test_attaches_the_selected_project(self, client: Client, triage_off: Any) -> None:
        project = ProjectFactory()

        client.post(
            "/security/upload/",
            {"zip_file": upload({"a.txt": REPORT_TEXT}), "project_id": str(project.pk)},
        )

        assert SecurityReport.objects.get().project_id == project.pk

    def test_missing_file_is_a_message_not_a_crash(self, client: Client, triage_off: Any) -> None:
        response = client.post("/security/upload/", {}, follow=True)

        assert response.status_code == 200
        assert b"Choose a .zip file" in response.content
        assert SecurityReport.objects.count() == 0

    def test_a_non_zip_upload_is_reported(self, client: Client, triage_off: Any) -> None:
        bogus = SimpleUploadedFile("reports.zip", b"definitely not a zip", "application/zip")

        response = client.post("/security/upload/", {"zip_file": bogus}, follow=True)

        assert b"not a valid zip archive" in response.content
        assert SecurityReport.objects.count() == 0

    def test_an_archive_with_no_reports_warns_rather_than_claiming_success(
        self, client: Client, triage_off: Any
    ) -> None:
        response = client.post(
            "/security/upload/",
            {"zip_file": upload({"screenshot.png": b"\x89PNG\r\n"})},
            follow=True,
        )

        assert b"Nothing imported" in response.content
        assert SecurityReport.objects.count() == 0

    def test_oversized_upload_is_refused_before_reading(
        self, client: Client, triage_off: Any
    ) -> None:
        from franktheunicorn.dashboard import views

        big = SimpleUploadedFile(
            "reports.zip", zip_bytes({"a.txt": REPORT_TEXT}), "application/zip"
        )
        # The view imports the importer inside the function, so the patch has to
        # land on the defining module rather than on views.
        with (
            patch.object(views, "MAX_SECURITY_ZIP_UPLOAD_BYTES", 4),
            patch("franktheunicorn.security.zip_import.import_reports_from_zip") as importer,
        ):
            response = client.post("/security/upload/", {"zip_file": big}, follow=True)

        assert b"upload limit" in response.content
        importer.assert_not_called()
        assert SecurityReport.objects.count() == 0

    def test_web_path_refuses_an_archive_too_big_for_one_request(
        self, client: Client, triage_off: Any
    ) -> None:
        """The import runs inside the request, so the web door caps entries well below MAX_ENTRIES."""
        from franktheunicorn.dashboard import views

        entries = {f"r{i}.txt": f"{REPORT_TEXT} variant {i}" for i in range(5)}
        with patch.object(views, "MAX_SYNCHRONOUS_ZIP_ENTRIES", 3):
            response = client.post("/security/upload/", {"zip_file": upload(entries)}, follow=True)

        assert b"over the 3 limit" in response.content
        assert b"better done from a shell" in response.content
        assert SecurityReport.objects.count() == 0

    def test_a_bad_operator_config_is_not_blamed_on_the_triage_setting(
        self, client: Client
    ) -> None:
        """Inferring "disabled in operator.yaml" from a count sent operators to a correct file."""
        with patch(
            "franktheunicorn.config.loader.get_operator_config",
            side_effect=ValueError("operator.yaml: bad indent on line 12"),
        ):
            response = client.post(
                "/security/upload/",
                {"zip_file": upload({"a.txt": REPORT_TEXT}), "auto_triage": "on"},
                follow=True,
            )

        body = response.content.decode()
        assert "could not read the operator config" in body
        assert "bad indent on line 12" in body
        assert "disabled in operator.yaml" not in body

    def test_a_non_numeric_project_id_is_a_message_not_a_500(
        self, client: Client, triage_off: Any
    ) -> None:
        response = client.post(
            "/security/upload/",
            {"zip_file": upload({"a.txt": REPORT_TEXT}), "project_id": "not-a-number"},
            follow=True,
        )

        assert response.status_code == 200
        # Autoescaped, so match around the apostrophe rather than through it.
        assert b"That project selection" in response.content
        assert SecurityReport.objects.count() == 0

    def test_success_and_failure_flashes_are_visually_distinct(
        self, client: Client, triage_off: Any
    ) -> None:
        """This diff added the first non-error messages; .flash hardcoded red."""
        ok = client.post(
            "/security/upload/", {"zip_file": upload({"a.txt": REPORT_TEXT})}, follow=True
        )
        bad = client.post("/security/upload/", {}, follow=True)

        assert b"flash-success" in ok.content
        assert b"flash-error" in bad.content

    def test_get_is_rejected(self, client: Client) -> None:
        assert client.get("/security/upload/").status_code == 405

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_upload_does_not_triage_unless_the_box_is_ticked(
        self, mock_config: MagicMock, client: Client
    ) -> None:
        from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True, auto_triage=True),
        )

        response = client.post(
            "/security/upload/", {"zip_file": upload({"a.txt": REPORT_TEXT})}, follow=True
        )

        assert WorkerCommand.objects.count() == 0
        assert b"imported untriaged" in response.content

    @patch("franktheunicorn.config.loader.get_operator_config")
    def test_ticking_the_box_queues_triage(self, mock_config: MagicMock, client: Client) -> None:
        from franktheunicorn.config.models import OperatorConfig, SecurityTriageConfig
        from franktheunicorn.core.models import WorkerCommand

        mock_config.return_value = OperatorConfig(
            github_username="testuser",
            security_triage=SecurityTriageConfig(enabled=True, auto_triage=True),
        )

        client.post(
            "/security/upload/",
            {"zip_file": upload({"a.txt": REPORT_TEXT}), "auto_triage": "on"},
        )

        assert WorkerCommand.objects.filter(command="run_security_triage").count() == 1

    def test_a_large_upload_goes_through_the_temp_file_path(
        self, client: Client, triage_off: Any
    ) -> None:
        """The real path for any realistic archive, and it wasn't covered.

        Django spills anything over FILE_UPLOAD_MAX_MEMORY_SIZE to a
        TemporaryUploadedFile on disk, so the importer sees a different file-like
        object than the in-memory one every other test exercises. Forcing the
        threshold to 0 is what actually reaches it — handing the test client a
        TemporaryUploadedFile does not, because the client re-encodes the body and
        the handler picks the class again by size.
        """
        from django.core.files.uploadedfile import TemporaryUploadedFile
        from django.test import override_settings

        seen: list[str] = []
        real = zip_import.import_reports_from_zip

        def recording(source: Any, **kwargs: Any) -> Any:
            seen.append(type(source).__name__)
            return real(source, **kwargs)

        entries = {f"r{i}.txt": f"{REPORT_TEXT} variant {i}" for i in range(3)}
        with (
            override_settings(FILE_UPLOAD_MAX_MEMORY_SIZE=0),
            patch.object(zip_import, "import_reports_from_zip", recording),
        ):
            response = client.post("/security/upload/", {"zip_file": upload(entries)}, follow=True)

        assert seen == [TemporaryUploadedFile.__name__]
        assert b"3 imported" in response.content
        assert SecurityReport.objects.count() == 3

    def test_many_failures_are_summarised_not_enumerated(
        self, client: Client, triage_off: Any
    ) -> None:
        """One flash per bad entry would bury the result in a page of noise."""
        from franktheunicorn.dashboard import views
        from franktheunicorn.security.zip_import import EntryOutcome, ZipImportResult

        stub = ZipImportResult(
            entries=[
                EntryOutcome(name=f"r{i}.txt", outcome="error", detail="boom") for i in range(40)
            ]
        )
        with patch(
            "franktheunicorn.security.zip_import.import_reports_from_zip", return_value=stub
        ):
            response = client.post(
                "/security/upload/", {"zip_file": upload({"a.txt": REPORT_TEXT})}, follow=True
            )

        body = response.content.decode()
        assert body.count("boom") == views.MAX_UPLOAD_ENTRY_MESSAGES
        assert "and 32 more" in body
