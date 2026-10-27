"""The auto-injected Magpie skill block: which prompts it rides, and the
per-project disable flag that gates it.

The block is the one door from the vendored Magpie text onto the agent
prompts. The properties that matter: it returns "" (not an empty section
heading) when nothing matches the path or the project disabled it; it filters
by ``inject_into`` so a security-triage rubric does not ride a general review
prompt; a file whose body is empty after the provenance header is dropped
rather than turning into a header with nothing under it; and the shipped lock
produces a non-empty block for every path the vendored files name.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from franktheunicorn.magpie import skills as skills_mod
from franktheunicorn.magpie.pin import MagpiePin, VendoredFile
from franktheunicorn.magpie.skills import skills_block, skills_block_for_project

SHA = "5dd9a1540a563e6027deb68d830d45b415268a0c"


def _pin(vendored: list[VendoredFile]) -> MagpiePin:
    return MagpiePin.model_validate(
        {
            "url": "https://github.com/apache/magpie.git",
            "commit": SHA,
            "commit_date": "2026-10-27",
            "vendored": vendored,
        }
    )


def _vf(dest: str, *, inject: list[str], body: str = "body text") -> VendoredFile:
    return VendoredFile.model_validate(
        {
            "source": f"plugins/x/{dest}",
            "dest": dest,
            "sha256": "0" * 64,
            "purpose": f"purpose for {dest}",
            "inject": inject,
        }
    )


class TestSkillsBlock:
    def test_disabled_returns_empty(self) -> None:
        with (
            patch.object(
                skills_mod, "load_pin", return_value=_pin([_vf("a.md", inject=["review"])])
            ),
            patch.object(skills_mod, "vendored_text", return_value="body"),
        ):
            assert skills_block(inject_into="review", enabled=False) == ""

    def test_no_matching_files_returns_empty(self) -> None:
        with (
            patch.object(
                skills_mod, "load_pin", return_value=_pin([_vf("a.md", inject=["security"])])
            ),
            patch.object(skills_mod, "vendored_text", return_value="body"),
        ):
            assert skills_block(inject_into="review") == ""

    def test_matching_files_are_concatenated_under_one_header(self) -> None:
        pin = _pin([_vf("a.md", inject=["review"]), _vf("b.md", inject=["review", "security"])])
        with (
            patch.object(skills_mod, "load_pin", return_value=pin),
            patch.object(skills_mod, "vendored_text", side_effect=["body a", "body b"]),
        ):
            block = skills_block(inject_into="review")
        assert "Borrowed Magpie skill text" in block
        assert "body a" in block
        assert "body b" in block
        # One header, not one per file.
        assert block.count("## Borrowed Magpie skill text") == 1

    def test_inject_filter_is_a_subset_match(self) -> None:
        """A file listed for both review and security rides both, but not verifier."""
        pin = _pin([_vf("shared.md", inject=["review", "security"])])
        with (
            patch.object(skills_mod, "load_pin", return_value=pin),
            patch.object(skills_mod, "vendored_text", return_value="shared body"),
        ):
            assert "shared body" in skills_block(inject_into="review")
            assert "shared body" in skills_block(inject_into="security")
            assert skills_block(inject_into="verifier") == ""

    def test_a_file_with_no_body_is_dropped(self) -> None:
        """An empty upstream stub must not become a section heading with nothing under it."""
        pin = _pin([_vf("empty.md", inject=["review"], body=""), _vf("real.md", inject=["review"])])
        with (
            patch.object(skills_mod, "load_pin", return_value=pin),
            patch.object(skills_mod, "vendored_text", side_effect=["", "real body"]),
        ):
            block = skills_block(inject_into="review")
        assert "real body" in block
        assert "empty.md" not in block

    def test_a_missing_file_is_skipped_not_fatal(self) -> None:
        pin = _pin([_vf("gone.md", inject=["review"]), _vf("here.md", inject=["review"])])
        with (
            patch.object(skills_mod, "load_pin", return_value=pin),
            # vendored_text logs a WARNING and returns "" for the missing one.
            patch.object(skills_mod, "vendored_text", side_effect=["", "here body"]),
        ):
            block = skills_block(inject_into="review")
        assert "here body" in block
        assert "gone.md" not in block

    def test_invalid_inject_into_is_a_caller_bug(self) -> None:
        with pytest.raises(ValueError, match="caller bug"):
            skills_block(inject_into="nonsense")


class TestShippedLock:
    def test_every_inject_entry_is_a_known_target(self) -> None:
        from franktheunicorn.magpie.pin import INJECT_TARGETS, load_pin

        pin = load_pin()
        for vf in pin.vendored:
            for target in vf.inject:
                assert target in INJECT_TARGETS

    def test_the_shipped_block_is_non_empty_for_each_named_path(self) -> None:
        """The three vendored files cover review, security, verifier, exploit —
        so the block is non-empty for every path the prompt builders ask for."""
        from franktheunicorn.magpie.pin import load_pin

        paths = sorted({target for vf in load_pin().vendored for target in vf.inject})
        for path in paths:
            assert skills_block(inject_into=path).strip(), f"empty block for {path!r}"

    def test_the_shipped_block_names_magpie_and_says_it_is_borrowed(self) -> None:
        block = skills_block(inject_into="review")
        assert "Apache Magpie" in block
        assert "borrowed" in block.lower() or "Borrowed" in block


class TestSkillsBlockForProject:
    def test_none_project_is_enabled_by_default(self) -> None:
        with patch.object(skills_mod, "skills_block", return_value="BLOCK") as sb:
            assert skills_block_for_project(None, inject_into="review") == "BLOCK"
        sb.assert_called_once_with(inject_into="review", enabled=True)

    def test_project_with_no_config_file_is_enabled(self) -> None:
        """A report nobody attached to a configured project still gets skills."""
        project = _FakeProject("orphan/repo")
        with (
            patch.object(skills_mod, "skills_block", return_value="BLOCK") as sb,
            patch("franktheunicorn.config.loader.get_project_config", return_value=None),
        ):
            assert skills_block_for_project(project, inject_into="verifier") == "BLOCK"
        sb.assert_called_once_with(inject_into="verifier", enabled=True)

    def test_project_with_magpie_skills_disabled_passes_the_flag(self) -> None:
        project = _FakeProject("apache/spark")
        pc = _FakeProjectConfig(magpie_skills_enabled=False)
        with (
            patch.object(skills_mod, "skills_block", return_value="") as sb,
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
        ):
            skills_block_for_project(project, inject_into="exploit")
        sb.assert_called_once_with(inject_into="exploit", enabled=False)

    def test_project_with_magpie_skills_enabled_passes_the_flag(self) -> None:
        project = _FakeProject("apache/spark")
        pc = _FakeProjectConfig(magpie_skills_enabled=True)
        with (
            patch.object(skills_mod, "skills_block", return_value="BLOCK") as sb,
            patch("franktheunicorn.config.loader.get_project_config", return_value=pc),
        ):
            skills_block_for_project(project, inject_into="security")
        sb.assert_called_once_with(inject_into="security", enabled=True)


class _FakeProject:
    """Just enough of core.models.Project for the resolver."""

    def __init__(self, full_name: str) -> None:
        self.full_name = full_name


class _FakeProjectConfig:
    """Just enough of config.models.ProjectConfig for the resolver."""

    def __init__(self, *, magpie_skills_enabled: bool) -> None:
        self.magpie_skills_enabled = magpie_skills_enabled
