"""Fetch the pinned Magpie revision and regenerate ``vendored/`` from it.

Everything here shells out to ``git`` and touches the network exactly once per
call (the fetch). It is developer tooling behind ``manage.py magpie_sync``; the
worker never imports it.

The fetch is a depth-one, blob-filtered fetch of one commit, optionally sparse:
Magpie's tree is 61 MB and the four directories frank reads are 6 MB. GitHub
serves arbitrary reachable shas, so the pin is fetched by sha and the result is
checked against the pin before anything is copied. A snapshot is never reused
across bumps: a bump fetches the new commit into a fresh directory.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path

import yaml

from franktheunicorn.magpie.pin import (
    MagpiePin,
    VendoredFile,
    provenance_header,
    sha256_bytes,
    split_provenance,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 600


class MagpieSyncError(RuntimeError):
    """A fetch or copy that cannot be completed as the lock describes."""


def _git(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    git: str = "git",
    timeout: int = _DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [git, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def fetch_snapshot(
    pin: MagpiePin,
    dest: Path,
    *,
    sparse_paths: Sequence[str] = (),
    git: str = "git",
    timeout: int = _DEFAULT_TIMEOUT,
) -> Path:
    """Materialise ``pin.commit`` under *dest* and return it.

    *dest* must not exist or must be an empty directory. With *sparse_paths* only
    those top-level directories (cone mode) are checked out; the root files
    (LICENSE, NOTICE, ...) always are. The blob filter is retried without when
    the server refuses it (local repositories without ``uploadpack.allowFilter``),
    so tests can point ``url`` at a path.
    """
    if dest.exists() and any(dest.iterdir()):
        msg = f"snapshot directory {dest} is not empty"
        raise MagpieSyncError(msg)
    dest.mkdir(parents=True, exist_ok=True)

    steps: list[list[str]] = [
        ["init", "-q"],
        ["remote", "add", "origin", pin.url],
    ]
    if sparse_paths:
        steps.append(["sparse-checkout", "init", "--cone"])
        steps.append(["sparse-checkout", "set", *sparse_paths])
    for args in steps:
        done = _git(args, cwd=dest, git=git, timeout=timeout)
        if done.returncode != 0:
            msg = f"git {' '.join(args)} failed in {dest}: {done.stderr.strip()[:300]}"
            raise MagpieSyncError(msg)

    fetch = ["fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", pin.commit]
    done = _git(fetch, cwd=dest, git=git, timeout=timeout)
    if done.returncode != 0 and "filter" in done.stderr.lower():
        logger.info("Remote refused the blob filter; fetching %s in full.", pin.short_commit)
        done = _git(
            [a for a in fetch if a != "--filter=blob:none"], cwd=dest, git=git, timeout=timeout
        )
    if done.returncode != 0:
        msg = f"could not fetch {pin.commit} from {pin.url}: {done.stderr.strip()[:300]}"
        raise MagpieSyncError(msg)

    done = _git(["checkout", "-q", "FETCH_HEAD"], cwd=dest, git=git, timeout=timeout)
    if done.returncode != 0:
        msg = f"git checkout FETCH_HEAD failed in {dest}: {done.stderr.strip()[:300]}"
        raise MagpieSyncError(msg)

    head = _git(["rev-parse", "HEAD"], cwd=dest, git=git, timeout=timeout)
    if head.returncode != 0 or head.stdout.strip() != pin.commit:
        msg = f"snapshot HEAD is {head.stdout.strip()!r}, pin is {pin.commit}"
        raise MagpieSyncError(msg)
    return dest


def sparse_paths_for(pin: MagpiePin) -> list[str]:
    """The top-level directories a sync needs: the parent dir of every source."""
    dirs: set[str] = set()
    for vf in pin.vendored:
        parts = Path(vf.source).parts
        if len(parts) > 1:
            dirs.add(str(Path(*parts[:-1])))
    return sorted(dirs)


def require_snapshot_at(
    snapshot: Path, commit: str, sources: Sequence[str] = (), *, git: str = "git"
) -> None:
    """Refuse *snapshot* unless it is a checkout of *commit* with *sources* unedited.

    ``sync_vendored`` writes *commit* into every provenance header and the
    lock, so the bytes it copies have to be that commit's. ``fetch_snapshot``
    proves that for the trees it fetches; a ``--snapshot-dir`` the operator
    points at is otherwise taken on trust, and a stale clone or a local edit
    would produce a lock that names one commit, hashes another's bytes, and
    passes ``--check`` (which compares the tree with the lock, not upstream).
    """
    head = _git(["rev-parse", "HEAD"], cwd=snapshot, git=git, timeout=60)
    if head.returncode != 0:
        msg = (
            f"{snapshot} is not a git checkout, so nothing shows it is "
            f"{commit[:12]}; point --snapshot-dir at a clone of that commit or omit it"
        )
        raise MagpieSyncError(msg)
    actual = head.stdout.strip()
    if actual != commit:
        msg = f"snapshot {snapshot} is at {actual[:12]}, not the pinned {commit[:12]}"
        raise MagpieSyncError(msg)
    if sources:
        status = _git(["status", "--porcelain", "--", *sources], cwd=snapshot, git=git, timeout=60)
        if status.returncode != 0 or status.stdout.strip():
            changed = status.stdout.strip() or status.stderr.strip()
            msg = f"snapshot {snapshot} has local changes to vendored sources: {changed[:300]}"
            raise MagpieSyncError(msg)


def sync_vendored(pin: MagpiePin, snapshot: Path, vendored_dir: Path) -> MagpiePin:
    """Copy every lock entry from *snapshot* into *vendored_dir*; return the lock
    with its ``sha256`` fields set to what was copied.

    Writes the provenance header ahead of the upstream bytes. Refuses a snapshot
    that is not a clean checkout of ``pin.commit`` (``require_snapshot_at``) and
    a source that is missing from it: an entry naming a file upstream renamed is
    exactly what a bump is supposed to surface. A file this function generated
    earlier whose entry has since left the lock is removed, so ``--check`` is
    clean after a ``--sync`` that dropped one; a markdown file without a
    provenance header was put there by hand and is left for ``--check`` to name.
    """
    require_snapshot_at(snapshot, pin.commit, [vf.source for vf in pin.vendored])
    vendored_dir.mkdir(parents=True, exist_ok=True)
    updated: list[VendoredFile] = []
    for vf in pin.vendored:
        src = snapshot / vf.source
        if not src.is_file():
            msg = f"{vf.source} does not exist at {pin.short_commit}; fix the lock entry"
            raise MagpieSyncError(msg)
        data = src.read_bytes()
        digest = sha256_bytes(data)
        new_entry = vf.model_copy(update={"sha256": digest})
        text = provenance_header(pin, new_entry) + data.decode("utf-8")
        (vendored_dir / vf.dest).write_text(text, encoding="utf-8")
        updated.append(new_entry)
        logger.info("vendored %s -> %s (%s)", vf.source, vf.dest, digest[:12])
    keep = {vf.dest for vf in pin.vendored}
    for entry in sorted(vendored_dir.glob("*.md")):
        if entry.name in keep or not entry.is_file():
            continue
        header, _ = split_provenance(entry.read_text(encoding="utf-8"))
        if header:
            entry.unlink()
            logger.info("removed %s: generated by an earlier sync, no longer in the lock", entry)
    return pin.model_copy(update={"vendored": updated})


def tag_at_commit(url: str, commit: str, *, git: str = "git", timeout: int = 60) -> str:
    """The tag whose (peeled) target is *commit*, or "". One ``ls-remote`` call."""
    done = _git(["ls-remote", "--tags", url], git=git, timeout=timeout)
    if done.returncode != 0:
        logger.warning("git ls-remote --tags %s failed: %s", url, done.stderr.strip()[:200])
        return ""
    best = ""
    for line in done.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if sha.strip() != commit or not ref.startswith("refs/tags/"):
            continue
        name = ref.removeprefix("refs/tags/").removesuffix("^{}")
        # Prefer the peeled (annotated) form when both appear; either names the tag.
        if ref.endswith("^{}") or not best:
            best = name
    return best


def commit_date(snapshot: Path, *, git: str = "git") -> str:
    done = _git(["log", "-1", "--format=%cs", "HEAD"], cwd=snapshot, git=git, timeout=60)
    return done.stdout.strip() if done.returncode == 0 else ""


_PIN_PREAMBLE = """\
# Pinned Apache Magpie revision: which upstream commit frank's vendored text and
# pinned tools come from. See docs/magpie-integration.md and
# src/franktheunicorn/magpie/pin.py.
#
# Edited by `manage.py magpie_sync --bump <commit>`, which also regenerates
# vendored/. Hand edits to `commit` without a --sync leave vendored/ stale, and
# `manage.py magpie_sync --check` (run in CI) will say so.
"""


def dump_pin(pin: MagpiePin) -> str:
    data = pin.model_dump(mode="json")
    return _PIN_PREAMBLE + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def write_pin(pin: MagpiePin, path: Path) -> None:
    path.write_text(dump_pin(pin), encoding="utf-8")


def bumped_pin(
    pin: MagpiePin,
    new_commit: str,
    snapshot: Path,
    *,
    git: str = "git",
) -> MagpiePin:
    """The lock re-pointed at *new_commit*, with tag and date resolved.

    *snapshot* must already be a checkout of *new_commit* (``fetch_snapshot`` with
    the new pin). The sha256 fields are left to ``sync_vendored``.
    """
    candidate = pin.model_copy(update={"commit": new_commit})
    return candidate.model_copy(
        update={
            "tag": tag_at_commit(pin.url, new_commit, git=git),
            "commit_date": commit_date(snapshot, git=git),
        }
    )
