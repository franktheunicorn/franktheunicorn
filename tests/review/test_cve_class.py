"""Tests for the CVE-class review check and its catalog."""

from __future__ import annotations

import pytest

from franktheunicorn.config.models import ProjectConfig
from franktheunicorn.review.checks.cve_class import CVEClassCheck
from franktheunicorn.security.cve_class_catalog import (
    SPARK_CVE_CLASSES,
    _render,
    resolve_cve_class_catalog,
)


def _pc(**overrides: object) -> ProjectConfig:
    base: dict[str, object] = {"owner": "apache", "repo": "spark"}
    base.update(overrides)
    return ProjectConfig(**base)  # type: ignore[arg-type]


class TestSparkCatalog:
    def test_has_six_classes(self) -> None:
        assert len(SPARK_CVE_CLASSES) == 6

    def test_every_class_has_cve_ids(self) -> None:
        for c in SPARK_CVE_CLASSES:
            assert c.cve_ids, f"{c.name} has no CVE ids"

    def test_classes_are_distinct(self) -> None:
        names = [c.name for c in SPARK_CVE_CLASSES]
        assert len(names) == len(set(names))

    def test_render_includes_name_look_for_and_cves(self) -> None:
        text = _render(SPARK_CVE_CLASSES)
        first = SPARK_CVE_CLASSES[0]
        assert first.name in text
        assert first.look_for in text
        assert first.cve_ids[0] in text


class TestResolveCatalog:
    def test_empty_when_not_configured(self) -> None:
        assert resolve_cve_class_catalog(_pc()) == ""

    def test_spark_builtin(self) -> None:
        text = resolve_cve_class_catalog(_pc(cve_class_catalog="spark"))
        assert "Command injection" in text
        assert "CVE-2022-33891" in text

    def test_unknown_builtin_key_is_empty(self) -> None:
        assert resolve_cve_class_catalog(_pc(cve_class_catalog="nope")) == ""

    def test_file_path(self, tmp_path: object) -> None:
        import pathlib

        f = pathlib.Path(str(tmp_path)) / "catalog.md"
        f.write_text("# My catalog\n- custom class\n")
        text = resolve_cve_class_catalog(_pc(cve_class_catalog=str(f)))
        assert "custom class" in text

    def test_missing_file_is_empty(self) -> None:
        assert resolve_cve_class_catalog(_pc(cve_class_catalog="/no/such/file")) == ""


class TestCVEClassCheck:
    def test_prompt_includes_catalog_when_present(self) -> None:
        check = CVEClassCheck(catalog="1. Test class\n   Look for: bad pattern\n")
        system, _user = check.build_prompt("diff", _pr_context())
        assert "Test class" in system
        assert "bad pattern" in system
        assert "cve-class:" in system

    def test_prompt_says_no_catalog_when_empty(self) -> None:
        check = CVEClassCheck(catalog="")
        system, _user = check.build_prompt("diff", _pr_context())
        assert "No CVE-class catalog is configured" in system

    def test_prompt_includes_security_model(self) -> None:
        check = CVEClassCheck(catalog="1. X\n   Look for: y\n")
        system, _user = check.build_prompt(
            "diff", _pr_context(security_model="trust boundary here")
        )
        assert "trust boundary here" in system

    def test_instantiating_without_catalog_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="franktheunicorn.review.checks.cve_class"):
            CVEClassCheck(catalog="")
        assert "no CVE-class catalog" in caplog.text


def _pr_context(security_model: str = "") -> object:
    from franktheunicorn.review.backends.base import PRContext

    return PRContext(
        pr_title="t",
        pr_body="",
        pr_author="someone",
        pr_number=1,
        project_name="apache/spark",
        review_context="general",
        review_style="direct",
        tone="direct",
        test_expectations="tests required",
        governance="asf",
        security_model=security_model,
    )


@pytest.mark.django_db
class TestCheckWiring:
    def test_registered_as_cve_class(self) -> None:
        from franktheunicorn.review.checks import _get_registry

        assert _get_registry()["cve-class"] is CVEClassCheck

    def test_instantiate_resolves_catalog(self) -> None:
        from franktheunicorn.review.checks import _instantiate_check

        check = _instantiate_check(
            CVEClassCheck, "cve-class", _pc(cve_class_catalog="spark"), None, None
        )
        assert isinstance(check, CVEClassCheck)
        assert "Command injection" in check.catalog

    def test_instantiate_empty_catalog_when_not_configured(self) -> None:
        from franktheunicorn.review.checks import _instantiate_check

        check = _instantiate_check(CVEClassCheck, "cve-class", _pc(), None, None)
        assert isinstance(check, CVEClassCheck)
        assert check.catalog == ""

    def test_config_accepts_cve_class_in_llm_checks(self) -> None:
        pc = _pc(llm_checks=["security", "cve-class"], cve_class_catalog="spark")
        assert "cve-class" in pc.llm_checks
