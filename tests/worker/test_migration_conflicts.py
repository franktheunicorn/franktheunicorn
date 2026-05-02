"""Tests for the worker-side migration conflict detector."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from franktheunicorn.worker.migration_conflicts import (
    MigrationConflict,
    _eval_dependencies,
    _parse_migration_dependencies,
    detect_migration_conflicts,
    is_django_project,
)

# Verbatim from ``django-admin startproject`` so any drift in the upstream
# template stays visible to a human reviewer rather than silently passing.
CANONICAL_MANAGE_PY = '''\
#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
'''


def _write_manage_py(repo: Path, *, content: str = CANONICAL_MANAGE_PY) -> Path:
    """Drop a manage.py at the repo root with the given content."""
    repo.mkdir(parents=True, exist_ok=True)
    target = repo / "manage.py"
    target.write_text(content)
    return target


def _write_migration(
    migrations_dir: Path,
    name: str,
    *,
    dependencies: list[tuple[str, str]],
) -> None:
    """Write a minimal Django migration file with the given dependencies list."""
    migrations_dir.mkdir(parents=True, exist_ok=True)
    init = migrations_dir / "__init__.py"
    if not init.exists():
        init.write_text("")
    deps_repr = ", ".join(f'("{app}", "{mig}")' for app, mig in dependencies)
    content = (
        "from django.db import migrations\n\n\n"
        "class Migration(migrations.Migration):\n"
        f"    dependencies = [{deps_repr}]\n"
        "    operations = []\n"
    )
    (migrations_dir / f"{name}.py").write_text(content)


@pytest.fixture
def django_repo(tmp_path: Path) -> Path:
    """A minimal repo that looks like Django: manage.py + one app with one migration."""
    repo = tmp_path / "django_repo"
    _write_manage_py(repo)
    _write_migration(repo / "core" / "migrations", "0001_initial", dependencies=[])
    return repo


# ---------------------------------------------------------------------------
# is_django_project
# ---------------------------------------------------------------------------


class TestIsDjangoProject:
    def test_detects_repo_with_canonical_manage_py(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write_manage_py(repo)
        assert is_django_project(repo) is True

    @pytest.mark.parametrize(
        "snippet",
        [
            "from django.core.management import execute_from_command_line",
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'app.settings')",
            "# Bootstrap script for the Django app\n",
            "raise ImportError('Couldn't import Django')\n",
            "FROM_DJANGO = True\n",
        ],
        ids=["import", "settings-env", "comment", "error-msg", "constant"],
    )
    def test_detects_any_manage_py_mentioning_django(self, tmp_path: Path, snippet: str) -> None:
        repo = tmp_path / "repo"
        _write_manage_py(repo, content=snippet)
        assert is_django_project(repo) is True

    @pytest.mark.parametrize(
        "snippet",
        [
            "#!/usr/bin/env python\n# Internal build helper.\nprint('hi')\n",
            "# manage.py shim for our custom build system\nimport sys\n",
            "",  # empty file
            "# fake manage.py\n",  # the placeholder used in the previous fixture
        ],
        ids=["build-helper", "custom-shim", "empty", "placeholder"],
    )
    def test_rejects_manage_py_without_django_reference(self, tmp_path: Path, snippet: str) -> None:
        repo = tmp_path / "repo"
        _write_manage_py(repo, content=snippet)
        assert is_django_project(repo) is False

    def test_manage_py_django_match_is_case_insensitive(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write_manage_py(
            repo,
            content="# DJANGO settings shim\nimport sys\nsys.exit(0)\n",
        )
        assert is_django_project(repo) is True

    def test_manage_py_must_be_a_regular_file(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        # A directory named manage.py is not a usable script.
        (repo / "manage.py").mkdir()
        assert is_django_project(repo) is False

    def test_unreadable_manage_py_falls_back_gracefully(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write_manage_py(repo)
        with patch(
            "franktheunicorn.worker.migration_conflicts.Path.open",
            side_effect=OSError("permission denied"),
        ):
            # Read failure for manage.py shouldn't crash; fall through to the
            # migrations-dir search, which finds nothing here.
            assert is_django_project(repo) is False

    def test_oversized_manage_py_with_django_far_in_file_still_detected(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        # 1 KiB of unrelated bytes followed by the django marker — well under
        # the 32 KiB read cap, so still detected.
        _write_manage_py(repo, content=("a" * 1024) + "\nimport django\n")
        assert is_django_project(repo) is True

    def test_oversized_manage_py_with_django_past_read_cap_is_missed(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        # Push the django reference past the 32 KiB read cap to confirm the
        # cap is honoured (defends against pathological multi-MB files).
        _write_manage_py(repo, content=("a" * (33 * 1024)) + "\nimport django\n")
        assert is_django_project(repo) is False

    def test_detects_repo_with_migrations_dir_only(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write_migration(repo / "myapp" / "migrations", "0001_initial", dependencies=[])
        assert is_django_project(repo) is True

    def test_detects_repo_with_nested_migrations(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write_migration(
            repo / "src" / "backend" / "users" / "migrations",
            "0001_initial",
            dependencies=[],
        )
        assert is_django_project(repo) is True

    def test_falls_through_to_migrations_when_manage_py_lacks_django(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        # A non-Django manage.py at the root, but the repo really is Django:
        # find the migrations-dir signal instead of giving up early.
        _write_manage_py(repo, content="# build script, not Django\n")
        _write_migration(repo / "core" / "migrations", "0001_initial", dependencies=[])
        assert is_django_project(repo) is True

    def test_rejects_empty_repo(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        assert is_django_project(repo) is False

    def test_rejects_python_repo_without_django(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.py").write_text("print('hello')\n")
        (repo / "lib").mkdir()
        (repo / "lib" / "utils.py").write_text("def f(): pass\n")
        assert is_django_project(repo) is False

    def test_rejects_nonexistent_path(self, tmp_path: Path) -> None:
        assert is_django_project(tmp_path / "does_not_exist") is False

    def test_rejects_when_repo_path_is_a_file(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "regular.py"
        not_a_dir.write_text("import django\n")
        assert is_django_project(not_a_dir) is False

    def test_ignores_migrations_in_venv(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.py").write_text("print('hi')\n")
        # A pip-installed Django package would have its own migrations under .venv.
        _write_migration(
            repo / ".venv" / "lib" / "site-packages" / "auth" / "migrations",
            "0001_initial",
            dependencies=[],
        )
        assert is_django_project(repo) is False

    @pytest.mark.parametrize(
        "skip_dir",
        ["node_modules", "__pycache__", "build", "dist", ".tox", ".eggs"],
    )
    def test_ignores_migrations_in_other_skip_dirs(self, tmp_path: Path, skip_dir: str) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_migration(
            repo / skip_dir / "vendored_app" / "migrations",
            "0001_initial",
            dependencies=[],
        )
        assert is_django_project(repo) is False

    def test_migrations_dir_without_init_does_not_count(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / "myapp" / "migrations").mkdir(parents=True)
        # No __init__.py — not a real Django migrations package.
        (repo / "myapp" / "migrations" / "0001_initial.py").write_text("")
        assert is_django_project(repo) is False


# ---------------------------------------------------------------------------
# detect_migration_conflicts
# ---------------------------------------------------------------------------


class TestDetectMigrationConflicts:
    def test_returns_empty_report_for_non_django_repo(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.py").write_text("print('hi')\n")
        report = detect_migration_conflicts(repo)
        assert report.is_django_project is False
        assert report.conflicts == []
        assert report.apps_scanned == 0

    def test_clean_linear_history_has_no_conflicts(self, django_repo: Path) -> None:
        _write_migration(
            django_repo / "core" / "migrations",
            "0002_add_field",
            dependencies=[("core", "0001_initial")],
        )
        _write_migration(
            django_repo / "core" / "migrations",
            "0003_add_index",
            dependencies=[("core", "0002_add_field")],
        )
        report = detect_migration_conflicts(django_repo)
        assert report.is_django_project is True
        assert report.conflicts == []
        assert report.apps_scanned == 1
        assert report.migrations_scanned == 3

    def test_detects_two_leaf_conflict(self, django_repo: Path) -> None:
        _write_migration(
            django_repo / "core" / "migrations",
            "0002_a",
            dependencies=[("core", "0001_initial")],
        )
        _write_migration(
            django_repo / "core" / "migrations",
            "0002_b",
            dependencies=[("core", "0001_initial")],
        )
        report = detect_migration_conflicts(django_repo)
        assert report.conflicts == [
            MigrationConflict(app_label="core", leaf_migrations=("0002_a", "0002_b")),
        ]

    def test_merge_migration_resolves_conflict(self, django_repo: Path) -> None:
        _write_migration(
            django_repo / "core" / "migrations",
            "0002_a",
            dependencies=[("core", "0001_initial")],
        )
        _write_migration(
            django_repo / "core" / "migrations",
            "0002_b",
            dependencies=[("core", "0001_initial")],
        )
        _write_migration(
            django_repo / "core" / "migrations",
            "0003_merge",
            dependencies=[("core", "0002_a"), ("core", "0002_b")],
        )
        report = detect_migration_conflicts(django_repo)
        assert report.conflicts == []

    def test_detects_conflicts_across_multiple_apps(self, django_repo: Path) -> None:
        _write_migration(
            django_repo / "core" / "migrations",
            "0002_a",
            dependencies=[("core", "0001_initial")],
        )
        _write_migration(
            django_repo / "core" / "migrations",
            "0002_b",
            dependencies=[("core", "0001_initial")],
        )
        _write_migration(
            django_repo / "billing" / "migrations",
            "0001_initial",
            dependencies=[],
        )
        _write_migration(
            django_repo / "billing" / "migrations",
            "0002_x",
            dependencies=[("billing", "0001_initial")],
        )
        _write_migration(
            django_repo / "billing" / "migrations",
            "0002_y",
            dependencies=[("billing", "0001_initial")],
        )
        report = detect_migration_conflicts(django_repo)
        assert report.conflicts == [
            MigrationConflict(app_label="billing", leaf_migrations=("0002_x", "0002_y")),
            MigrationConflict(app_label="core", leaf_migrations=("0002_a", "0002_b")),
        ]
        assert report.apps_scanned == 2

    def test_cross_app_dependencies_do_not_count_as_same_app_parents(
        self, django_repo: Path
    ) -> None:
        # A migration in `billing` depending on `core.0001` must not turn
        # `core.0001` into a non-leaf when we check the `billing` graph,
        # nor make `billing.0001` look like it has parents in core.
        _write_migration(
            django_repo / "billing" / "migrations",
            "0001_initial",
            dependencies=[("core", "0001_initial")],
        )
        report = detect_migration_conflicts(django_repo)
        assert report.conflicts == []
        assert report.apps_scanned == 2

    def test_handles_unparseable_migration(self, django_repo: Path) -> None:
        (django_repo / "core" / "migrations" / "0002_broken.py").write_text(
            "this is not valid python ::: \n"
        )
        report = detect_migration_conflicts(django_repo)
        assert report.is_django_project is True
        assert report.conflicts == []
        # Unparseable migration is skipped, not counted.
        assert report.migrations_scanned == 1

    def test_skips_init_and_non_python_files(self, django_repo: Path) -> None:
        (django_repo / "core" / "migrations" / "README.md").write_text("notes\n")
        (django_repo / "core" / "migrations" / "helpers.txt").write_text("ignored\n")
        report = detect_migration_conflicts(django_repo)
        assert report.migrations_scanned == 1

    def test_three_way_leaf_conflict(self, django_repo: Path) -> None:
        for suffix in ("a", "b", "c"):
            _write_migration(
                django_repo / "core" / "migrations",
                f"0002_{suffix}",
                dependencies=[("core", "0001_initial")],
            )
        report = detect_migration_conflicts(django_repo)
        assert len(report.conflicts) == 1
        conflict = report.conflicts[0]
        assert conflict.app_label == "core"
        assert conflict.leaf_migrations == ("0002_a", "0002_b", "0002_c")

    def test_app_with_only_init_is_skipped(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        (repo / "core" / "migrations").mkdir(parents=True)
        (repo / "core" / "migrations" / "__init__.py").write_text("")
        report = detect_migration_conflicts(repo)
        assert report.is_django_project is True
        assert report.apps_scanned == 0
        assert report.conflicts == []

    def test_parses_real_django_generated_migration(self, tmp_path: Path) -> None:
        """Smoke-test against a copy of an actual ``makemigrations`` output."""
        repo = tmp_path / "repo"
        _write_manage_py(repo)
        migrations_dir = repo / "core" / "migrations"
        migrations_dir.mkdir(parents=True)
        (migrations_dir / "__init__.py").write_text("")
        # Verbatim shape of a migration from the franktheunicorn tree, with
        # imports, model creation, FK, and the canonical dependencies block.
        (migrations_dir / "0001_initial.py").write_text(
            "# Generated by Django 5.2.13 on 2026-04-29 22:04\n"
            "\n"
            "import django.db.models.deletion\n"
            "import django.utils.timezone\n"
            "from django.db import migrations, models\n"
            "\n"
            "\n"
            "class Migration(migrations.Migration):\n"
            "    initial = True\n"
            "\n"
            "    dependencies = []\n"
            "\n"
            "    operations = [\n"
            "        migrations.CreateModel(\n"
            "            name='Widget',\n"
            "            fields=[\n"
            "                ('id', models.BigAutoField(primary_key=True)),\n"
            "                ('label', models.CharField(max_length=100)),\n"
            "                ('created_at',\n"
            "                 models.DateTimeField(default=django.utils.timezone.now)),\n"
            "            ],\n"
            "        ),\n"
            "    ]\n"
        )
        (migrations_dir / "0002_real.py").write_text(
            "from django.db import migrations\n"
            "\n"
            "\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [\n"
            "        ('core', '0001_initial'),\n"
            "    ]\n"
            "    operations = []\n"
        )
        report = detect_migration_conflicts(repo)
        assert report.is_django_project is True
        assert report.conflicts == []
        assert report.migrations_scanned == 2

    def test_dependencies_declared_as_tuple_are_accepted(self, django_repo: Path) -> None:
        """Older / hand-written migrations sometimes use a tuple instead of a list."""
        (django_repo / "core" / "migrations" / "0002_tuple_deps.py").write_text(
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = (\n"
            "        ('core', '0001_initial'),\n"
            "    )\n"
            "    operations = []\n"
        )
        report = detect_migration_conflicts(django_repo)
        assert report.conflicts == []
        # 0002_tuple_deps now chains after 0001_initial → still a single leaf.
        assert report.migrations_scanned == 2

    def test_migration_class_with_other_name_is_ignored(self, django_repo: Path) -> None:
        """We only look at the canonical ``class Migration`` body."""
        (django_repo / "core" / "migrations" / "0002_renamed.py").write_text(
            "from django.db import migrations\n\n\n"
            "class NotAMigration:\n"
            "    dependencies = [('core', '0001_initial')]\n"
            "    operations = []\n"
        )
        report = detect_migration_conflicts(django_repo)
        # File has no Migration class → not a real migration, dropped before
        # the leaf-conflict check. Only 0001_initial is counted.
        assert report.conflicts == []
        assert report.migrations_scanned == 1

    def test_migration_with_no_dependencies_attr_is_treated_as_root(
        self, django_repo: Path
    ) -> None:
        (django_repo / "core" / "migrations" / "0002_no_deps.py").write_text(
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    operations = []\n"
        )
        report = detect_migration_conflicts(django_repo)
        # Two roots, both leaves → reported as a conflict.
        assert len(report.conflicts) == 1
        assert report.conflicts[0].leaf_migrations == ("0001_initial", "0002_no_deps")


# ---------------------------------------------------------------------------
# Internal parser helpers (direct coverage)
# ---------------------------------------------------------------------------


class TestParseMigrationDependencies:
    def test_extracts_typical_dependency_list(self, tmp_path: Path) -> None:
        path = tmp_path / "0002_thing.py"
        path.write_text(
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [\n"
            "        ('core', '0001_initial'),\n"
            "        ('billing', '0002_charges'),\n"
            "    ]\n"
            "    operations = []\n"
        )
        assert _parse_migration_dependencies(path) == [
            ("core", "0001_initial"),
            ("billing", "0002_charges"),
        ]

    def test_returns_none_on_unreadable_file(self, tmp_path: Path) -> None:
        assert _parse_migration_dependencies(tmp_path / "missing.py") is None

    def test_returns_none_on_syntax_error(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.py"
        path.write_text("class Migration\n  no colon, syntax error :::\n")
        assert _parse_migration_dependencies(path) is None

    def test_returns_none_when_no_migration_class(self, tmp_path: Path) -> None:
        path = tmp_path / "module.py"
        path.write_text("dependencies = [('core', '0001_initial')]\n")
        assert _parse_migration_dependencies(path) is None

    def test_returns_empty_list_when_migration_class_omits_dependencies(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "0001_initial.py"
        path.write_text(
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    initial = True\n"
            "    operations = []\n"
        )
        # Django defaults ``dependencies`` to ``[]`` — match that, don't
        # silently drop the file.
        assert _parse_migration_dependencies(path) == []


class TestEvalDependencies:
    def _expr(self, source: str) -> object:
        import ast as _ast

        return _ast.parse(source, mode="eval").body

    def test_extracts_string_tuple_pairs(self) -> None:
        node = self._expr("[('a', 'b'), ('c', 'd')]")
        assert _eval_dependencies(node) == [("a", "b"), ("c", "d")]

    def test_skips_non_tuple_elements(self) -> None:
        node = self._expr("[('a', 'b'), 'just-a-string', ('c', 'd')]")
        assert _eval_dependencies(node) == [("a", "b"), ("c", "d")]

    def test_skips_tuples_with_non_string_constants(self) -> None:
        # Django-generated migrations are always (str, str), but be defensive
        # against hand-edited files that wrap deps in swappable_dependency() etc.
        node = self._expr("[(1, 2), ('app', 'name')]")
        assert _eval_dependencies(node) == [("app", "name")]

    def test_returns_empty_list_for_non_collection(self) -> None:
        node = self._expr("'not a list'")
        assert _eval_dependencies(node) == []
