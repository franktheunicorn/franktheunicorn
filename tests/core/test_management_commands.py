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
    # add_project now also registers the Project row (so analyze_repo works
    # on a fresh install), hence the db marker.
    @pytest.mark.django_db
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

    @pytest.mark.django_db
    def test_shows_analyze_hint(self, tmp_path: Path) -> None:
        out = StringIO()
        call_command(
            "add_project",
            "--repo=testorg/testrepo",
            f"--output-dir={tmp_path}",
            stdout=out,
        )
        assert "analyze_repo" in out.getvalue()

    @pytest.mark.django_db
    def test_registers_project_row(self, tmp_path: Path) -> None:
        """analyze_repo needs the Project row; add_project must create it."""
        from franktheunicorn.core.models import Project

        call_command(
            "add_project",
            "--repo=testorg/testrepo",
            f"--output-dir={tmp_path}",
            stdout=StringIO(),
        )
        assert Project.objects.filter(owner="testorg", repo="testrepo").exists()

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
class TestAnalyzeRepoCommand:
    def test_invalid_repo_format(self) -> None:
        err = StringIO()
        call_command("analyze_repo", "--repo=invalid", stderr=err)
        assert "owner/repo" in err.getvalue()

    def test_project_not_found(self) -> None:
        err = StringIO()
        call_command("analyze_repo", "--repo=no/such-project", stderr=err)
        assert "not found" in err.getvalue()

    def test_skips_if_recent_snapshot(self) -> None:
        from django.utils import timezone

        project = ProjectFactory()
        project.repo_health_analyzed_at = timezone.now()
        project.save()
        out = StringIO()
        call_command("analyze_repo", f"--repo={project.owner}/{project.repo}", stdout=out)
        assert "Existing snapshot" in out.getvalue()

    def test_force_re_analyzes(self, tmp_path: Path) -> None:
        from django.utils import timezone

        from franktheunicorn.worker.repo_health import RepoHealthSnapshot, snapshot_to_dict

        project = ProjectFactory()
        project.repo_health_analyzed_at = timezone.now()
        project.repo_health_snapshot = snapshot_to_dict(RepoHealthSnapshot(analyzed_at="old"))
        project.save()

        # Create a real git repo for ensure_repo to find
        import subprocess

        repo_path = tmp_path / project.owner / project.repo
        repo_path.mkdir(parents=True)
        subprocess.run(["git", "init", str(repo_path)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "config", "user.email", "t@t.com"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "config", "user.name", "Tester"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )
        (repo_path / "f.py").write_text("x = 1\n")
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "add", "."],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m", "init"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )

        out = StringIO()
        with patch(
            "franktheunicorn.worker.repo_manager.ensure_repo",
            return_value=repo_path,
        ):
            call_command(
                "analyze_repo",
                f"--repo={project.owner}/{project.repo}",
                "--force",
                stdout=out,
            )
        output = out.getvalue()
        assert "Repo health analysis" in output
        assert "Snapshot saved" in output

    def test_clone_failure(self) -> None:
        project = ProjectFactory()
        err = StringIO()
        with patch(
            "franktheunicorn.worker.repo_manager.ensure_repo",
            return_value=None,
        ):
            call_command(
                "analyze_repo",
                f"--repo={project.owner}/{project.repo}",
                stderr=err,
            )
        assert "Failed to clone" in err.getvalue()


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


class TestLoginFromEmail:
    """Collaborator keys must be forge-login-ish so they can match
    PullRequest.author during scoring — not space-containing display names."""

    def test_github_noreply_plain(self) -> None:
        from franktheunicorn.core.management.commands.detect_collaborators import (
            _login_from_email,
        )

        assert _login_from_email("janedoe@users.noreply.github.com") == "janedoe"

    def test_github_noreply_id_prefixed(self) -> None:
        from franktheunicorn.core.management.commands.detect_collaborators import (
            _login_from_email,
        )

        assert _login_from_email("12345+janedoe@users.noreply.github.com") == "janedoe"

    def test_plain_email_local_part(self) -> None:
        from franktheunicorn.core.management.commands.detect_collaborators import (
            _login_from_email,
        )

        assert _login_from_email("Jane.Doe@example.com") == "jane.doe"

    def test_non_email_returns_empty(self) -> None:
        from franktheunicorn.core.management.commands.detect_collaborators import (
            _login_from_email,
        )

        assert _login_from_email("Jane Doe") == ""


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
class TestWorkerStatusCommand:
    def test_runs_without_error(self) -> None:
        out = StringIO()
        call_command("worker_status", stdout=out)
        output = out.getvalue()
        assert "Open PRs" in output


@pytest.mark.django_db
class TestCheckRateLimitsCommand:
    def test_no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FRANK_GITHUB_TOKEN", raising=False)
        out = StringIO()
        call_command("check_rate_limits", stdout=out)
        assert "not set" in out.getvalue()


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
