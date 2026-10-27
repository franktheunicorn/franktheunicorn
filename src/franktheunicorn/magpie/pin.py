"""The pinned Apache Magpie revision and what frank takes from it.

Why a pin at all. Magpie lands many commits a day and has shipped one release
(``0.1.0``, July 2026) that predates most of the pieces frank borrows. So every
borrowed piece is taken from an exact commit, recorded in ``magpie-pin.yaml``
together with a hash of every vendored file. That buys three things:

* a bump is a reviewable diff of one commit range, not "whatever main is";
* ``manage.py magpie_sync --check`` can prove, offline and in CI, that the
  vendored text still matches what the lock says it is;
* nothing in this codebase ever fetches a floating ref.

Three ways a piece is consumed, one lock for all of them:

* **Vendored text.** Markdown copied into ``vendored/`` with a provenance header
  naming the source path and commit. Used as prompt material, so it needs
  nothing installed anywhere and works with every agent CLI and every remote
  executor. :func:`vendored_text` returns the body without the header.
* **Worker-side Python tools.** Installed by ``pip`` from the git URL at the
  pinned sha plus ``#subdirectory=tools/<name>``; :meth:`PinnedTool.requirement`
  is the exact requirement string. They run on the worker host before anything
  leaves it.
* **Agent-host tools.** Run with ``uvx --from git+<url>@<sha>#subdirectory=...``
  on whichever host runs the agent CLI, which in the maintainer's deployment is
  a remote box reached over SSH (``RemoteSSHExecutor``). :meth:`PinnedTool.uvx_argv`
  builds that one-line argv; only ``uv`` has to exist on the host, and a host
  without it degrades (logged, skipped) rather than failing the review.

The pin file is data, edited only by ``manage.py magpie_sync --bump``; the
vendored files are generated, never hand-edited.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

#: Where the lock lives. Packaged with the code (not operator config) because it
#: describes which upstream revision the code's own vendored text came from.
PIN_PATH: Path = Path(__file__).resolve().parent / "magpie-pin.yaml"

#: Where vendored upstream files land. Each carries a provenance header.
VENDORED_DIR: Path = Path(__file__).resolve().parent / "vendored"

#: Last line of the provenance header. Everything after it is the upstream
#: content byte for byte, which is what the lock's ``sha256`` is computed over.
PROVENANCE_END = "END MAGPIE PROVENANCE -->"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: The prompt paths a vendored file may be auto-injected into. Kept here so a
#: typo in the lock is a parse error, not a silently-never-injected entry.
INJECT_TARGETS: frozenset[str] = frozenset(
    {"review", "security", "verifier", "exploit", "cve_match"}
)


class PinnedTool(BaseModel):
    """One Magpie Python tool frank runs, pinned by subdirectory.

    ``name`` is the package name in ``tools/<dir>/pyproject.toml`` (they differ
    from the directory for a few, e.g. ``magpie-gitlab`` in ``tools/gitlab``).
    ``console_script`` is the entry point to invoke; it defaults to ``name``.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    subdirectory: str
    console_script: str = ""
    #: Why frank runs it, in one line, so the lock is readable on its own.
    purpose: str = ""

    @property
    def script(self) -> str:
        return self.console_script or self.name

    def requirement(self, pin: MagpiePin) -> str:
        """The PEP 508 requirement that installs exactly this tool at the pin."""
        return f"{self.name} @ {pin.git_source(self.subdirectory)}"

    def uvx_argv(self, pin: MagpiePin, *args: str) -> list[str]:
        """``uvx`` argv that runs the tool at the pin with no pre-install.

        One argv, no process stdin, so it survives every delivery mode of the
        remote SSH executor. ``uv`` caches the build, so only the first run on a
        host pays for the fetch.
        """
        return ["uvx", "--from", pin.git_source(self.subdirectory), self.script, *args]


class VendoredFile(BaseModel):
    """One upstream file copied into ``vendored/``.

    ``sha256`` is over the upstream bytes at the pinned commit, which equals the
    vendored file's body after the provenance header. Empty until the first
    ``--sync`` fills it in.

    ``inject`` names the agent prompts this file is auto-injected into as
    Magpie skill text (see :func:`franktheunicorn.magpie.skills.skills_block`).
    Empty means the file is vendored for other uses (a tool reads it) but does
    not ride a prompt. The allowed values are :data:`INJECT_TARGETS`.
    """

    model_config = ConfigDict(frozen=True)

    #: Path inside the Magpie tree.
    source: str
    #: File name under ``vendored/``.
    dest: str
    sha256: str = ""
    #: What frank uses it for, in one line.
    purpose: str = ""
    #: Which agent prompts this skill text rides: subset of ``INJECT_TARGETS``.
    #: Tuple, not list, so a frozen pin stays frozen (a list field stays
    #: mutable in place on a frozen model).
    inject: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("source", "dest")
    @classmethod
    def _relative_and_sane(cls, v: str) -> str:
        v = v.strip()
        if not v or v.startswith("/") or ".." in Path(v).parts:
            msg = f"vendored path must be relative and inside the tree, got {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("sha256")
    @classmethod
    def _sha256_shape(cls, v: str) -> str:
        v = v.strip().lower()
        if v and not _SHA256_RE.match(v):
            msg = f"sha256 must be 64 hex characters, got {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("inject")
    @classmethod
    def _inject_targets(cls, v: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        out: list[str] = []
        for item in v:
            name = item.strip()
            if name not in INJECT_TARGETS:
                msg = (
                    f"inject entry {name!r} is not one of {sorted(INJECT_TARGETS)}; "
                    "fix the lock entry"
                )
                raise ValueError(msg)
            if name not in out:
                out.append(name)
        return tuple(out)


class MagpiePin(BaseModel):
    """The lock: one upstream commit and everything taken from it."""

    model_config = ConfigDict(frozen=True)

    url: str = "https://github.com/apache/magpie.git"
    commit: str
    #: The release tag that points at ``commit``, or "" when the pin is ahead of
    #: every tag, which is the normal state while Magpie ships daily.
    tag: str = ""
    #: ``YYYY-MM-DD`` of the commit, for a human reading the lock.
    commit_date: str = ""
    tools: list[PinnedTool] = Field(default_factory=list)
    vendored: list[VendoredFile] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def _https_only(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("https://") or v.startswith("file://") or v.startswith("/")):
            msg = f"magpie pin url must be https:// (or a local path for tests), got {v!r}"
            raise ValueError(msg)
        return v

    @field_validator("commit")
    @classmethod
    def _full_sha(cls, v: str) -> str:
        v = v.strip().lower()
        if not _SHA_RE.match(v):
            msg = f"magpie pin commit must be a full 40-hex sha, got {v!r}"
            raise ValueError(msg)
        return v

    @property
    def short_commit(self) -> str:
        return self.commit[:12]

    def git_source(self, subdirectory: str) -> str:
        """``git+<url>@<sha>#subdirectory=<dir>`` as pip and uv both read it."""
        url = self.url if "://" in self.url else f"file://{self.url}"
        return f"git+{url}@{self.commit}#subdirectory={subdirectory.strip('/')}"

    def compare_url(self, new_commit: str) -> str:
        """The GitHub compare page for a bump, for the reviewer of the bump."""
        base = self.url.removesuffix(".git")
        return f"{base}/compare/{self.commit}...{new_commit}"

    def tool(self, name: str) -> PinnedTool | None:
        for t in self.tools:
            if t.name == name:
                return t
        return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_pin(path: Path = PIN_PATH) -> MagpiePin:
    """Parse the lock. Raises on a malformed file: a bad pin is a code bug."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return MagpiePin.model_validate(data)


def provenance_header(pin: MagpiePin, vendored: VendoredFile) -> str:
    """The comment that opens every vendored file.

    Apache-2.0 §4 asks that redistributed source keep its notices; the SPDX line
    Magpie puts at the top of its own files survives in the body, and this header
    adds the exact origin so a reader can diff against upstream without guessing.
    """
    when = f" ({pin.commit_date})" if pin.commit_date else ""
    return (
        "<!-- Vendored from Apache Magpie (https://github.com/apache/magpie)\n"
        f"     source:  {vendored.source}\n"
        f"     commit:  {pin.commit}{when}\n"
        "     license: Apache-2.0 (https://www.apache.org/licenses/LICENSE-2.0)\n"
        "     Generated by `manage.py magpie_sync`; do not edit by hand.\n"
        f"     {PROVENANCE_END}\n"
    )


def split_provenance(text: str) -> tuple[str, str]:
    """``(header, body)``. ``header`` is "" when the file carries none."""
    marker = PROVENANCE_END + "\n"
    at = text.find(marker)
    if at < 0 or not text.startswith("<!-- Vendored from Apache Magpie"):
        return "", text
    end = at + len(marker)
    return text[:end], text[end:]


def vendored_text(dest: str, vendored_dir: Path = VENDORED_DIR) -> str:
    """The upstream body of a vendored file, without the provenance header.

    Returns "" and logs at WARNING when the file is missing: the callers are
    prompt builders, and a review without a borrowed criteria block is better
    than no review. A missing file here means the lock and the tree disagree,
    which ``manage.py magpie_sync --check`` reports in full.
    """
    path = vendored_dir / dest
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning(
            "Vendored Magpie file %s is missing; continuing without it. "
            "Run `manage.py magpie_sync --sync` to regenerate vendored/ from the pin.",
            path,
        )
        return ""
    _header, body = split_provenance(text)
    return body


def verify_vendored(pin: MagpiePin, vendored_dir: Path = VENDORED_DIR) -> list[str]:
    """Every way ``vendored/`` can disagree with the lock, as human-readable lines.

    Offline by construction: it compares the tree against the lock, never
    against upstream. An empty list means the tree is exactly what the lock
    says. A non-empty one is a failed CI check.
    """
    problems: list[str] = []
    expected: set[str] = set()
    for vf in pin.vendored:
        expected.add(vf.dest)
        path = vendored_dir / vf.dest
        if not path.is_file():
            problems.append(f"{vf.dest}: missing (source {vf.source})")
            continue
        text = path.read_text(encoding="utf-8")
        header, body = split_provenance(text)
        if not header:
            problems.append(f"{vf.dest}: no provenance header")
        elif pin.commit not in header:
            problems.append(f"{vf.dest}: provenance names a different commit than the pin")
        if not vf.sha256:
            problems.append(f"{vf.dest}: lock has no sha256 (run --sync)")
        elif sha256_bytes(body.encode("utf-8")) != vf.sha256:
            problems.append(f"{vf.dest}: body does not match the lock's sha256")
    if vendored_dir.is_dir():
        for entry in sorted(vendored_dir.iterdir()):
            if entry.is_file() and entry.name not in expected and entry.suffix == ".md":
                generated = bool(split_provenance(entry.read_text(encoding="utf-8"))[0])
                problems.append(
                    f"{entry.name}: present in vendored/ but not in the lock"
                    + (
                        " (left by an earlier sync; --sync removes it)"
                        if generated
                        else " (no provenance header, so not generated; delete it or add a lock entry)"
                    )
                )
    return problems
