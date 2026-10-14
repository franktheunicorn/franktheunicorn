"""Tests for the final-release tag rule ported from Apache Magpie."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps

from franktheunicorn.security.release_tags import (
    FINAL,
    OTHER,
    PRERELEASE,
    ReleaseTagRule,
    classify_tag,
    is_final_release_tag,
    release_tag_rule_for,
)


@pytest.mark.parametrize(
    ("tag", "prefix", "kind"),
    [
        ("v3.5.0", "", FINAL),
        ("3.5.0", "", FINAL),
        ("v3.5", "", FINAL),
        ("V4.0.0", "", FINAL),
        ("2.11.0.post1", "", FINAL),
        ("2.11.0-post2", "", FINAL),
        ("v3.5.0-rc1", "", PRERELEASE),
        ("2.11.0rc1", "", PRERELEASE),
        ("v4.0.0-preview1", "", PRERELEASE),
        ("3.0.0b1", "", PRERELEASE),
        ("release-1.18.0", "", OTHER),
        ("rel/1.2.3", "", OTHER),
        ("providers-amazon/8.0.0", "", OTHER),
        ("docs-snapshot", "", OTHER),
        ("3", "", OTHER),
        ("v0.9.0-incubating", "", FINAL),
        ("0.9.0-incubating-rc2", "", PRERELEASE),
        ("release-1.18.0", "release-", FINAL),
        ("release-1.18.0-rc2", "release-", PRERELEASE),
        ("rel/1.2.3", "rel/", FINAL),
        ("v3.5.0", "rel/", OTHER),
    ],
)
def test_classify_tag(tag: str, prefix: str, kind: str) -> None:
    assert classify_tag(tag, prefix) == kind
    assert is_final_release_tag(tag, prefix) is (kind == FINAL)


@pytest.mark.django_db
def test_the_prefix_comes_from_the_project_yaml(tmp_path: Path, settings: Any) -> None:
    (tmp_path / "apache-flink.yaml").write_text(
        "owner: apache\nrepo: flink\nrelease_tag_prefix: 'release-'\n"
    )
    settings.FRANK_PROJECTS_DIR = str(tmp_path)

    assert release_tag_rule_for("apache/flink") == ReleaseTagRule(prefix="release-")
    assert release_tag_rule_for("apache/spark") == ReleaseTagRule()


def test_a_pattern_replaces_the_version_test() -> None:
    rule = ReleaseTagRule(pattern=r"STRUTS_\d+(_\d+)+")
    assert rule.classify("STRUTS_7_4_0") == FINAL
    assert rule.classify("STRUTS_7_4_0_RC1") == OTHER
    assert rule.classify("v7.4.0") == PRERELEASE  # version-shaped, not this project's finals


def test_a_bad_pattern_is_refused_at_config_load() -> None:
    from pydantic import ValidationError

    from franktheunicorn.config.models import ProjectConfig

    with pytest.raises(ValidationError, match="release_tag_pattern"):
        ProjectConfig(owner="a", repo="b", release_tag_pattern="(unclosed")


@pytest.mark.django_db
def test_migration_reopens_releases_that_only_a_candidate_supported() -> None:
    """``released`` is terminal, so rows the old rule stamped would otherwise
    never be looked at again."""
    from tests.factories import SecurityReportFactory

    migration = importlib.import_module(
        "franktheunicorn.core.migrations.0064_reopen_fix_landed_released_by_candidates"
    )
    candidate = SecurityReportFactory(
        fix_landed_status="released",
        fix_landed_method="git",
        fix_landed_detail={"tags": ["v3.5.0-rc1"], "tag_count": 1},
    )
    final = SecurityReportFactory(
        fix_landed_status="released",
        fix_landed_method="git",
        fix_landed_detail={"tags": ["v3.5.0", "v3.5.0-rc1"], "tag_count": 2},
    )
    agent = SecurityReportFactory(
        fix_landed_status="released",
        fix_landed_method="agent",
        fix_landed_detail={"tags": ["v3.5.0-rc1"]},
    )

    migration.reopen_candidate_releases(apps, None)

    for report in (candidate, final, agent):
        report.refresh_from_db()
    # Downgraded, not emptied: a tag proves merged, and an empty status would
    # let an indeterminate non-answer replace what git already proved.
    assert candidate.fix_landed_status == "merged"
    assert candidate.fix_landed_method == "git"
    assert "downgraded from released" in candidate.fix_landed_detail["note"]
    assert candidate.fix_landed_detail["other_tags"] == ["v3.5.0-rc1"]
    assert "tags" not in candidate.fix_landed_detail
    assert final.fix_landed_status == "released"
    assert agent.fix_landed_status == "released"
