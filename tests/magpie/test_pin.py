"""The lock format, the provenance header, and the offline verifier.

The properties that matter: the shipped lock parses and names a full sha; the
requirement and uvx strings are exactly the shapes pip and uv accept; the
verifier catches every way the tree can drift from the lock and is silent when
it has not; and a missing vendored file degrades to an empty prompt block with a
WARNING rather than an exception in the review path.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from franktheunicorn.magpie import pin as pin_mod
from franktheunicorn.magpie.pin import (
    PIN_PATH,
    MagpiePin,
    PinnedTool,
    VendoredFile,
    load_pin,
    provenance_header,
    sha256_bytes,
    split_provenance,
    vendored_text,
    verify_vendored,
)

SHA = "dadb315caa8b881fc1d2195bef03223dac25915a"


def _pin(**overrides: object) -> MagpiePin:
    base: dict[str, object] = {
        "url": "https://github.com/apache/magpie.git",
        "commit": SHA,
        "commit_date": "2026-10-11",
        "tools": [PinnedTool(name="agent-guard", subdirectory="tools/agent-guard")],
        "vendored": [
            VendoredFile(
                source="plugins/magpie-pr-management/skills/code-review/criteria/testing.md",
                dest="pr-code-review-criteria-testing.md",
            )
        ],
    }
    base.update(overrides)
    return MagpiePin.model_validate(base)


class TestShippedLock:
    def test_the_packaged_lock_parses(self) -> None:
        pin = load_pin(PIN_PATH)
        assert len(pin.commit) == 40
        assert pin.url.startswith("https://github.com/apache/magpie")

    def test_every_vendored_entry_in_the_lock_is_on_disk_and_consistent(self) -> None:
        # The real tree against the real lock: this is the CI check, run as a test
        # too so a stale vendored/ cannot slip past a developer who skipped the hook.
        assert verify_vendored(load_pin(PIN_PATH)) == []


class TestShapes:
    def test_commit_must_be_a_full_sha(self) -> None:
        with pytest.raises(ValidationError):
            _pin(commit="dadb315")

    def test_url_must_be_https(self) -> None:
        with pytest.raises(ValidationError):
            _pin(url="http://github.com/apache/magpie.git")

    def test_vendored_paths_stay_inside_the_tree(self) -> None:
        with pytest.raises(ValidationError):
            VendoredFile(source="../etc/passwd", dest="x.md")
        with pytest.raises(ValidationError):
            VendoredFile(source="a.md", dest="/tmp/x.md")

    def test_sha256_shape(self) -> None:
        with pytest.raises(ValidationError):
            VendoredFile(source="a.md", dest="a.md", sha256="not-hex")

    def test_requirement_is_pip_shaped(self) -> None:
        pin = _pin()
        tool = pin.tool("agent-guard")
        assert tool is not None
        assert tool.requirement(pin) == (
            f"agent-guard @ git+https://github.com/apache/magpie.git@{SHA}#subdirectory=tools/agent-guard"
        )

    def test_uvx_argv_is_one_argv_with_the_pin(self) -> None:
        pin = _pin()
        tool = pin.tool("agent-guard")
        assert tool is not None
        assert tool.uvx_argv(pin, "--opencode") == [
            "uvx",
            "--from",
            f"git+https://github.com/apache/magpie.git@{SHA}#subdirectory=tools/agent-guard",
            "agent-guard",
            "--opencode",
        ]

    def test_console_script_defaults_to_the_package_name(self) -> None:
        assert PinnedTool(name="pii-tools", subdirectory="x").script == "pii-tools"
        assert PinnedTool(
            name="redactor", subdirectory="x", console_script="pii-redact"
        ).script == ("pii-redact")

    def test_unknown_tool_is_none(self) -> None:
        assert _pin().tool("nope") is None

    def test_local_path_url_is_wrapped_as_file_scheme_for_git_sources(self) -> None:
        pin = _pin(url="/srv/magpie")
        assert pin.git_source("tools/x") == f"git+file:///srv/magpie@{SHA}#subdirectory=tools/x"

    def test_compare_url_drops_the_dot_git(self) -> None:
        assert _pin().compare_url("f" * 40) == (
            f"https://github.com/apache/magpie/compare/{SHA}...{'f' * 40}"
        )


class TestProvenance:
    def test_header_round_trips_and_names_the_pin(self) -> None:
        pin = _pin()
        header = provenance_header(pin, pin.vendored[0])
        body = "# Testing\n\nbody\n"
        got_header, got_body = split_provenance(header + body)
        assert got_header == header
        assert got_body == body
        assert SHA in header
        assert pin.vendored[0].source in header
        assert "Apache-2.0" in header

    def test_text_without_a_header_is_all_body(self) -> None:
        assert split_provenance("# plain\n") == ("", "# plain\n")

    def test_a_lookalike_header_that_does_not_open_the_file_is_not_a_header(self) -> None:
        text = "# plain\n<!-- Vendored from Apache Magpie\n     END MAGPIE PROVENANCE -->\nx\n"
        assert split_provenance(text) == ("", text)


class TestVerify:
    def _write_consistent(self, pin: MagpiePin, vendored_dir: Path) -> MagpiePin:
        vf = pin.vendored[0]
        body = "# Testing\n\nbody\n"
        digest = sha256_bytes(body.encode("utf-8"))
        locked = pin.model_copy(update={"vendored": [vf.model_copy(update={"sha256": digest})]})
        vendored_dir.mkdir(parents=True, exist_ok=True)
        (vendored_dir / vf.dest).write_text(
            provenance_header(locked, locked.vendored[0]) + body, encoding="utf-8"
        )
        return locked

    def test_consistent_tree_has_no_problems(self, tmp_path: Path) -> None:
        pin = self._write_consistent(_pin(), tmp_path / "v")
        assert verify_vendored(pin, tmp_path / "v") == []

    def test_missing_file(self, tmp_path: Path) -> None:
        pin = self._write_consistent(_pin(), tmp_path / "v")
        (tmp_path / "v" / pin.vendored[0].dest).unlink()
        [problem] = verify_vendored(pin, tmp_path / "v")
        assert "missing" in problem

    def test_edited_body(self, tmp_path: Path) -> None:
        pin = self._write_consistent(_pin(), tmp_path / "v")
        path = tmp_path / "v" / pin.vendored[0].dest
        path.write_text(path.read_text(encoding="utf-8") + "hand edit\n", encoding="utf-8")
        [problem] = verify_vendored(pin, tmp_path / "v")
        assert "sha256" in problem

    def test_header_from_another_commit(self, tmp_path: Path) -> None:
        pin = self._write_consistent(_pin(), tmp_path / "v")
        path = tmp_path / "v" / pin.vendored[0].dest
        path.write_text(path.read_text(encoding="utf-8").replace(SHA, "e" * 40), encoding="utf-8")
        [problem] = verify_vendored(pin, tmp_path / "v")
        assert "different commit" in problem

    def test_no_header_at_all(self, tmp_path: Path) -> None:
        pin = self._write_consistent(_pin(), tmp_path / "v")
        path = tmp_path / "v" / pin.vendored[0].dest
        path.write_text("# Testing\n\nbody\n", encoding="utf-8")
        problems = verify_vendored(pin, tmp_path / "v")
        assert any("no provenance header" in p for p in problems)

    def test_lock_without_hash_is_reported(self, tmp_path: Path) -> None:
        pin = _pin()
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / pin.vendored[0].dest).write_text(
            provenance_header(pin, pin.vendored[0]) + "x\n", encoding="utf-8"
        )
        [problem] = verify_vendored(pin, tmp_path / "v")
        assert "no sha256" in problem

    def test_stray_markdown_in_vendored_dir_is_reported(self, tmp_path: Path) -> None:
        pin = self._write_consistent(_pin(), tmp_path / "v")
        (tmp_path / "v" / "orphan.md").write_text("x\n", encoding="utf-8")
        [problem] = verify_vendored(pin, tmp_path / "v")
        assert problem.startswith("orphan.md")


class TestVendoredText:
    def test_returns_the_body_without_the_header(self, tmp_path: Path) -> None:
        pin = _pin()
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "a.md").write_text(
            provenance_header(pin, pin.vendored[0]) + "# A\n", encoding="utf-8"
        )
        assert vendored_text("a.md", tmp_path / "v") == "# A\n"

    def test_missing_file_is_empty_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=pin_mod.__name__):
            assert vendored_text("nope.md", tmp_path) == ""
        assert "magpie_sync --sync" in caplog.text
