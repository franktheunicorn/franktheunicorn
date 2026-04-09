"""Tests for the check_migration_conflicts management command."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command


class TestCheckMigrationConflicts:
    def test_no_conflicts(self) -> None:
        out = StringIO()
        call_command("check_migration_conflicts", stdout=out)
        assert "No migration conflicts detected" in out.getvalue()

    def test_detects_leaf_node_conflicts(self) -> None:
        fake_conflicts = {
            "core": ["0005_reviewdraft_code_context", "0005_v15_context_fields"],
        }
        with patch(
            "franktheunicorn.core.management.commands.check_migration_conflicts.MigrationLoader"
        ) as mock_loader_cls:
            mock_loader_cls.return_value.detect_conflicts.return_value = fake_conflicts
            err = StringIO()
            with pytest.raises(SystemExit, match="1"):
                call_command("check_migration_conflicts", stderr=err)
            output = err.getvalue()
            assert "core" in output
            assert "0005_reviewdraft_code_context" in output
            assert "0005_v15_context_fields" in output
            assert "makemigrations --merge" in output

    def test_detects_conflicts_in_multiple_apps(self) -> None:
        fake_conflicts = {
            "core": ["0010_a", "0010_b"],
            "dashboard": ["0002_x", "0002_y"],
        }
        with patch(
            "franktheunicorn.core.management.commands.check_migration_conflicts.MigrationLoader"
        ) as mock_loader_cls:
            mock_loader_cls.return_value.detect_conflicts.return_value = fake_conflicts
            err = StringIO()
            with pytest.raises(SystemExit, match="1"):
                call_command("check_migration_conflicts", stderr=err)
            output = err.getvalue()
            assert "core" in output
            assert "dashboard" in output
            assert "2 migration issue(s) found" in output

    def test_check_unmade_flag_clean(self) -> None:
        """--check-unmade passes when models match migrations."""
        out = StringIO()
        call_command("check_migration_conflicts", "--check-unmade", stdout=out)
        assert "No migration conflicts detected" in out.getvalue()

    def test_check_unmade_detects_drift(self) -> None:
        fake_change = MagicMock()
        fake_changes = {"core": [fake_change]}
        with (
            patch(
                "franktheunicorn.core.management.commands.check_migration_conflicts.MigrationLoader"
            ) as mock_loader_cls,
            patch(
                "franktheunicorn.core.management.commands.check_migration_conflicts.MigrationAutodetector"
            ) as mock_autodetector_cls,
        ):
            mock_loader = mock_loader_cls.return_value
            mock_loader.detect_conflicts.return_value = {}
            mock_autodetector_cls.return_value.changes.return_value = fake_changes
            err = StringIO()
            with pytest.raises(SystemExit, match="1"):
                call_command("check_migration_conflicts", "--check-unmade", stderr=err)
            output = err.getvalue()
            assert "Unmade migration" in output
            assert "core" in output
            assert "makemigrations core" in output

    def test_skips_unmade_check_by_default(self) -> None:
        """Without --check-unmade, only leaf-node conflicts are checked."""
        with patch(
            "franktheunicorn.core.management.commands.check_migration_conflicts.MigrationLoader"
        ) as mock_loader_cls:
            mock_loader_cls.return_value.detect_conflicts.return_value = {}
            out = StringIO()
            call_command("check_migration_conflicts", stdout=out)
            assert "No migration conflicts detected" in out.getvalue()

    def test_exit_code_zero_on_success(self) -> None:
        """Command does not raise SystemExit on success."""
        out = StringIO()
        # Should not raise
        call_command("check_migration_conflicts", stdout=out)
