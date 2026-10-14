"""Check, regenerate or bump the pinned Apache Magpie vendored files.

``--check`` (the default) is offline and is what CI runs: it compares
``vendored/`` against the lock. ``--sync`` fetches the pinned commit into a
temporary directory and regenerates ``vendored/`` from it. ``--bump <sha>``
re-points the lock at another commit, syncs, and prints the upstream compare
URL so the bump can be reviewed as the diff it is.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from franktheunicorn.magpie.pin import PIN_PATH, VENDORED_DIR, MagpiePin, load_pin, verify_vendored
from franktheunicorn.magpie.sync import (
    MagpieSyncError,
    bumped_pin,
    fetch_snapshot,
    sparse_paths_for,
    sync_vendored,
    write_pin,
)


class Command(BaseCommand):
    help = "Verify, regenerate or bump frank's pinned Apache Magpie vendored files."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--check",
            action="store_true",
            help="verify vendored/ against the lock, offline (default when nothing else is given)",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help="fetch the pinned commit and regenerate vendored/ from it",
        )
        parser.add_argument(
            "--bump",
            metavar="COMMIT",
            default="",
            help="re-point the lock at this full 40-hex commit, then sync",
        )
        parser.add_argument(
            "--snapshot-dir",
            metavar="DIR",
            default="",
            help="an existing checkout of the target commit to copy from instead of fetching",
        )
        parser.add_argument(
            "--pin-file", metavar="PATH", default=str(PIN_PATH), help="lock file (tests)"
        )
        parser.add_argument(
            "--vendored-dir", metavar="DIR", default=str(VENDORED_DIR), help="output dir (tests)"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        pin_path = Path(options["pin_file"])
        vendored_dir = Path(options["vendored_dir"])
        pin = load_pin(pin_path)

        if options["bump"]:
            self._bump(pin, options["bump"], pin_path, vendored_dir, options["snapshot_dir"])
            return
        if options["sync"]:
            self._sync(pin, pin_path, vendored_dir, options["snapshot_dir"])
            return
        self._check(pin, vendored_dir)

    def _check(self, pin: MagpiePin, vendored_dir: Path) -> None:
        problems = verify_vendored(pin, vendored_dir)
        if problems:
            for line in problems:
                self.stderr.write(f"  {line}")
            msg = (
                f"vendored/ disagrees with the Magpie pin ({pin.short_commit}) in "
                f"{len(problems)} place(s); run `manage.py magpie_sync --sync`"
            )
            raise CommandError(msg)
        self.stdout.write(
            f"Magpie pin {pin.short_commit}"
            f"{' (' + pin.tag + ')' if pin.tag else ''}: "
            f"{len(pin.vendored)} vendored file(s), {len(pin.tools)} tool(s), all consistent."
        )

    def _sync(self, pin: MagpiePin, pin_path: Path, vendored_dir: Path, snapshot_dir: str) -> None:
        try:
            new_pin = self._materialise(pin, vendored_dir, snapshot_dir)
        except MagpieSyncError as exc:
            raise CommandError(str(exc)) from exc
        write_pin(new_pin, pin_path)
        self.stdout.write(
            f"Regenerated {len(new_pin.vendored)} vendored file(s) at {new_pin.short_commit}."
        )

    def _bump(
        self, pin: MagpiePin, new_commit: str, pin_path: Path, vendored_dir: Path, snapshot_dir: str
    ) -> None:
        try:
            target = pin.model_copy(update={"commit": new_commit.strip().lower()})
            target = MagpiePin.model_validate(target.model_dump())
            with tempfile.TemporaryDirectory(prefix="magpie-bump-") as tmp:
                snapshot = (
                    Path(snapshot_dir)
                    if snapshot_dir
                    else fetch_snapshot(
                        target, Path(tmp) / "snapshot", sparse_paths=sparse_paths_for(target)
                    )
                )
                target = bumped_pin(pin, target.commit, snapshot)
                new_pin = sync_vendored(target, snapshot, vendored_dir)
        except (MagpieSyncError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        write_pin(new_pin, pin_path)
        self.stdout.write(
            f"Bumped Magpie pin {pin.short_commit} -> {new_pin.short_commit}"
            f"{' (' + new_pin.tag + ')' if new_pin.tag else ''}; "
            f"{len(new_pin.vendored)} vendored file(s) regenerated.\n"
            f"Review the upstream range before committing: {pin.compare_url(new_pin.commit)}"
        )

    @staticmethod
    def _materialise(pin: MagpiePin, vendored_dir: Path, snapshot_dir: str) -> MagpiePin:
        if snapshot_dir:
            return sync_vendored(pin, Path(snapshot_dir), vendored_dir)
        with tempfile.TemporaryDirectory(prefix="magpie-sync-") as tmp:
            snapshot = fetch_snapshot(
                pin, Path(tmp) / "snapshot", sparse_paths=sparse_paths_for(pin)
            )
            return sync_vendored(pin, snapshot, vendored_dir)
