"""Fetching a pinned snapshot and regenerating vendored/ from it, offline.

Against a local stand-in repository (see conftest). The properties: a fetch is
by sha and checked against the pin; a sparse fetch still copies what the lock
names; the copy writes a header and fills in the hash so the verifier is then
silent; a renamed upstream file fails loudly instead of vendoring nothing; a bump
resolves the tag only when one points at the commit; and the management command
wires it all with the right exit codes.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from franktheunicorn.magpie.pin import (
    MagpiePin,
    VendoredFile,
    load_pin,
    split_provenance,
    verify_vendored,
)
from franktheunicorn.magpie.sync import (
    MagpieSyncError,
    bumped_pin,
    dump_pin,
    fetch_snapshot,
    sparse_paths_for,
    sync_vendored,
    tag_at_commit,
    write_pin,
)
from tests.magpie.conftest import FakeUpstream

TESTING = "plugins/magpie-pr-management/skills/code-review/criteria/testing.md"
DISPOSITIONS = "plugins/magpie-security/skills/issue-triage/dispositions.md"


def _pin(up: FakeUpstream, commit: str = "") -> MagpiePin:
    return MagpiePin(
        url=str(up.path),
        commit=commit or up.first,
        vendored=[
            VendoredFile(source=TESTING, dest="testing.md"),
            VendoredFile(source=DISPOSITIONS, dest="dispositions.md"),
        ],
    )


class TestFetchSnapshot:
    def test_fetches_exactly_the_pinned_commit(
        self, upstream: FakeUpstream, tmp_path: Path
    ) -> None:
        snap = fetch_snapshot(_pin(upstream), tmp_path / "snap")
        assert (snap / TESTING).read_text(encoding="utf-8") == upstream.files[TESTING].replace(
            " Neither is one that passes on the base branch.", ""
        )
        assert (snap / "NOTICE").is_file()

    def test_sparse_fetch_limits_the_tree_but_keeps_root_files(
        self, upstream: FakeUpstream, tmp_path: Path
    ) -> None:
        pin = _pin(upstream)
        paths = sparse_paths_for(pin)
        assert paths == [
            "plugins/magpie-pr-management/skills/code-review/criteria",
            "plugins/magpie-security/skills/issue-triage",
        ]
        snap = fetch_snapshot(pin, tmp_path / "snap", sparse_paths=paths)
        assert (snap / TESTING).is_file()
        assert (snap / DISPOSITIONS).is_file()
        assert (snap / "NOTICE").is_file()

    def test_refuses_a_non_empty_destination(self, upstream: FakeUpstream, tmp_path: Path) -> None:
        dest = tmp_path / "snap"
        dest.mkdir()
        (dest / "junk").write_text("x", encoding="utf-8")
        with pytest.raises(MagpieSyncError, match="not empty"):
            fetch_snapshot(_pin(upstream), dest)

    def test_unknown_commit_fails_loudly(self, upstream: FakeUpstream, tmp_path: Path) -> None:
        with pytest.raises(MagpieSyncError, match="could not fetch"):
            fetch_snapshot(_pin(upstream, "a" * 40), tmp_path / "snap")


class TestSyncVendored:
    def test_copies_with_header_and_fills_in_hashes(
        self, upstream: FakeUpstream, tmp_path: Path
    ) -> None:
        pin = _pin(upstream)
        snap = fetch_snapshot(pin, tmp_path / "snap")
        locked = sync_vendored(pin, snap, tmp_path / "v")
        assert all(len(vf.sha256) == 64 for vf in locked.vendored)
        header, body = split_provenance((tmp_path / "v" / "testing.md").read_text(encoding="utf-8"))
        assert upstream.first in header
        assert TESTING in header
        assert body.startswith("<!-- SPDX-License-Identifier: Apache-2.0 -->")
        assert verify_vendored(locked, tmp_path / "v") == []

    def test_a_source_missing_upstream_is_an_error(
        self, upstream: FakeUpstream, tmp_path: Path
    ) -> None:
        pin = _pin(upstream).model_copy(
            update={"vendored": [VendoredFile(source="plugins/renamed.md", dest="r.md")]}
        )
        snap = fetch_snapshot(pin, tmp_path / "snap")
        with pytest.raises(MagpieSyncError, match="does not exist"):
            sync_vendored(pin, snap, tmp_path / "v")

    def test_lock_round_trips_through_yaml(self, upstream: FakeUpstream, tmp_path: Path) -> None:
        pin = _pin(upstream)
        write_pin(pin, tmp_path / "pin.yaml")
        assert load_pin(tmp_path / "pin.yaml") == pin
        assert dump_pin(pin).startswith("# Pinned Apache Magpie revision")


class TestBump:
    def test_tag_resolves_only_at_the_tagged_commit(self, upstream: FakeUpstream) -> None:
        assert tag_at_commit(str(upstream.path), upstream.first) == "0.1.0"
        assert tag_at_commit(str(upstream.path), upstream.second) == ""

    def test_bumped_pin_carries_tag_date_and_new_commit(
        self, upstream: FakeUpstream, tmp_path: Path
    ) -> None:
        pin = _pin(upstream)
        new = pin.model_copy(update={"commit": upstream.second})
        snap = fetch_snapshot(new, tmp_path / "snap")
        bumped = bumped_pin(pin, upstream.second, snap)
        assert bumped.commit == upstream.second
        assert bumped.tag == ""
        assert bumped.commit_date == "2026-10-11"
        sync_vendored(bumped, snap, tmp_path / "v")
        _h, body = split_provenance((tmp_path / "v" / "testing.md").read_text(encoding="utf-8"))
        assert "base branch" in body


@pytest.mark.django_db
class TestCommand:
    def _write_pin(self, upstream: FakeUpstream, tmp_path: Path) -> Path:
        pin_path = tmp_path / "pin.yaml"
        write_pin(_pin(upstream), pin_path)
        return pin_path

    def test_check_fails_before_the_first_sync_and_passes_after(
        self, upstream: FakeUpstream, tmp_path: Path
    ) -> None:
        pin_path = self._write_pin(upstream, tmp_path)
        vendored = tmp_path / "v"
        with pytest.raises(CommandError, match="disagrees"):
            call_command("magpie_sync", pin_file=str(pin_path), vendored_dir=str(vendored))

        out = StringIO()
        call_command(
            "magpie_sync", sync=True, pin_file=str(pin_path), vendored_dir=str(vendored), stdout=out
        )
        assert "Regenerated 2 vendored file(s)" in out.getvalue()

        out = StringIO()
        call_command(
            "magpie_sync",
            check=True,
            pin_file=str(pin_path),
            vendored_dir=str(vendored),
            stdout=out,
        )
        assert "all consistent" in out.getvalue()
        assert load_pin(pin_path).vendored[0].sha256

    def test_sync_from_an_existing_snapshot_dir(
        self, upstream: FakeUpstream, tmp_path: Path
    ) -> None:
        pin_path = self._write_pin(upstream, tmp_path)
        snap = fetch_snapshot(load_pin(pin_path), tmp_path / "snap")
        out = StringIO()
        call_command(
            "magpie_sync",
            sync=True,
            snapshot_dir=str(snap),
            pin_file=str(pin_path),
            vendored_dir=str(tmp_path / "v"),
            stdout=out,
        )
        assert "Regenerated" in out.getvalue()

    def test_bump_rewrites_the_lock_and_prints_the_compare_url(
        self, upstream: FakeUpstream, tmp_path: Path
    ) -> None:
        pin_path = self._write_pin(upstream, tmp_path)
        out = StringIO()
        call_command(
            "magpie_sync",
            bump=upstream.second,
            pin_file=str(pin_path),
            vendored_dir=str(tmp_path / "v"),
            stdout=out,
        )
        text = out.getvalue()
        assert f"-> {upstream.second[:12]}" in text
        assert f"/compare/{upstream.first}...{upstream.second}" in text
        assert load_pin(pin_path).commit == upstream.second
        assert verify_vendored(load_pin(pin_path), tmp_path / "v") == []

    def test_bump_rejects_a_short_sha(self, upstream: FakeUpstream, tmp_path: Path) -> None:
        pin_path = self._write_pin(upstream, tmp_path)
        with pytest.raises(CommandError, match="40-hex"):
            call_command(
                "magpie_sync",
                bump="abc123",
                pin_file=str(pin_path),
                vendored_dir=str(tmp_path / "v"),
            )

    def test_sync_failure_is_a_command_error(self, upstream: FakeUpstream, tmp_path: Path) -> None:
        pin_path = tmp_path / "pin.yaml"
        write_pin(_pin(upstream, "b" * 40), pin_path)
        with pytest.raises(CommandError, match="could not fetch"):
            call_command(
                "magpie_sync", sync=True, pin_file=str(pin_path), vendored_dir=str(tmp_path)
            )
