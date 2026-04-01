"""Tests for management commands."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from franktheunicorn.core.models import Project
from tests.factories import (
    OperatorActionFactory,
    ProjectFactory,
    PullRequestFactory,
    ReviewDraftFactory,
)


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


@pytest.mark.django_db
class TestTrainRejectionModelCommand:
    def _create_actions(self, project: Project, count: int) -> None:
        pr = PullRequestFactory(project=project, additions=50, deletions=10)
        for i in range(count):
            draft = ReviewDraftFactory(
                pull_request=pr,
                category="style" if i % 2 == 0 else "correctness",
                file_path=f"src/m{i}.py",
            )
            OperatorActionFactory(
                action_type="accept_draft" if i % 3 != 0 else "reject_draft",
                review_draft=draft,
                pull_request=pr,
            )

    def test_no_data(self, db_project: Project) -> None:
        out = StringIO()
        call_command(
            "train_rejection_model",
            f"--project={db_project.full_name}",
            stdout=out,
        )
        output = out.getvalue()
        assert "Skipping" in output or "No actions" in output

    def test_sufficient_data(self, db_project: Project, tmp_path: Path) -> None:
        self._create_actions(db_project, 55)
        out = StringIO()
        model_path = tmp_path / "models" / "apache-spark" / "rejection_model.pkl"
        with patch(
            "franktheunicorn.core.management.commands.train_rejection_model.model_path_for_project",
            return_value=model_path,
        ):
            call_command(
                "train_rejection_model",
                f"--project={db_project.full_name}",
                stdout=out,
            )
        output = out.getvalue()
        assert "Trained" in output
        assert model_path.exists()

    def test_insufficient_data_skipped(self, db_project: Project) -> None:
        self._create_actions(db_project, 10)
        out = StringIO()
        call_command(
            "train_rejection_model",
            f"--project={db_project.full_name}",
            stdout=out,
        )
        output = out.getvalue()
        assert "Skipping" in output

    def test_force_flag(self, db_project: Project, tmp_path: Path) -> None:
        self._create_actions(db_project, 10)
        out = StringIO()
        model_path = tmp_path / "models" / "apache-spark" / "rejection_model.pkl"
        with patch(
            "franktheunicorn.core.management.commands.train_rejection_model.model_path_for_project",
            return_value=model_path,
        ):
            call_command(
                "train_rejection_model",
                f"--project={db_project.full_name}",
                "--force",
                stdout=out,
            )
        output = out.getvalue()
        assert "Trained" in output

    def test_invalid_project_format(self) -> None:
        err = StringIO()
        call_command(
            "train_rejection_model",
            "--project=invalid",
            stderr=err,
        )
        assert "owner/repo" in err.getvalue()

    def test_nonexistent_project(self) -> None:
        err = StringIO()
        call_command(
            "train_rejection_model",
            "--project=nonexistent/project",
            stderr=err,
        )
        assert "not found" in err.getvalue()

    def test_train_all(self, db_project: Project, tmp_path: Path) -> None:
        self._create_actions(db_project, 55)
        out = StringIO()
        model_path = tmp_path / "models" / "apache-spark" / "rejection_model.pkl"
        with patch(
            "franktheunicorn.core.management.commands.train_rejection_model.model_path_for_project",
            return_value=model_path,
        ):
            call_command("train_rejection_model", stdout=out)
        output = out.getvalue()
        assert "Trained" in output


@pytest.mark.django_db
class TestExportTrainingDataCommand:
    def _create_actions(self, project: Project, count: int) -> None:
        for _ in range(count):
            pr = PullRequestFactory(project=project)
            draft = ReviewDraftFactory(
                pull_request=pr,
                comment_body="Great approach. Consider adding error handling.",
            )
            OperatorActionFactory(
                action_type="accept_draft",
                review_draft=draft,
                pull_request=pr,
            )

    def test_export_with_force(self, tmp_path: Path) -> None:
        project = ProjectFactory(owner="test", repo="export")
        self._create_actions(project, 5)
        out = StringIO()
        call_command(
            "export_training_data",
            "--project=test/export",
            f"--output-dir={tmp_path / 'out'}",
            "--force",
            stdout=out,
        )
        output = out.getvalue()
        assert "Exported" in output
        assert (tmp_path / "out" / "train.jsonl").exists()

    def test_nonexistent_project(self) -> None:
        with pytest.raises(Exception, match="not found"):
            call_command(
                "export_training_data",
                "--project=nonexistent/project",
            )

    def test_insufficient_data_without_force(self) -> None:
        project = ProjectFactory(owner="test", repo="small")
        self._create_actions(project, 3)
        with pytest.raises(Exception, match="Not enough"):
            call_command(
                "export_training_data",
                "--project=test/small",
            )


@pytest.mark.django_db
class TestFineTuneCommand:
    def test_export_failure_stops_pipeline(self) -> None:
        ProjectFactory(owner="test", repo="empty")
        with pytest.raises(Exception, match=r"Not enough|No operator"):
            call_command("fine_tune", "--project=test/empty")

    def test_nonexistent_project(self) -> None:
        with pytest.raises(Exception, match="not found"):
            call_command("fine_tune", "--project=nonexistent/project")
