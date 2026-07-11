"""Backport / cherry-pick faithfulness sub-check.

When a PR declares itself a backport or cherry-pick of another PR or commit,
this check fetches the ORIGINAL (source) diff, compares it against the
backport PR's diff, and flags SEEMING DIFFERENCES so a human can eyeball
whether the backport faithfully mirrors the original.

It is a deterministic, non-LLM check: detection is regex based and the
comparison is a diff-of-diffs over :class:`unidiff.PatchSet`. When the PR is
not a declared backport the check is a silent no-op (no findings).

Enable via::

    llm_checks: ["backport"]

and (optionally) tune the ``backport:`` config block (ignore_paths, which
divergence classes to warn on, etc.).

The source diff is fetched the forge-aware way: the check receives the
project's own ``ForgeClient`` (GitHub / GHE / Gitea / GitLab) so private and
self-hosted repos get the right auth, rather than public-scrape-only. PR
references use ``get_pull_request_diff``; commit-SHA references use
``get_commit_diff``.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from unidiff import PatchSet  # type: ignore[import-untyped]

from franktheunicorn.data_access.github.issue_fetcher import ISSUE_REF_PATTERN
from franktheunicorn.review.backends.base import ReviewFinding
from franktheunicorn.review.checks import BaseCheck

if TYPE_CHECKING:
    from franktheunicorn.backends.base import ForgeClient
    from franktheunicorn.config.models import BackportConfig, LLMBackendConfig
    from franktheunicorn.core.models import PullRequest
    from franktheunicorn.review.backends.base import PRContext

logger = logging.getLogger(__name__)


# --- reference detection ---------------------------------------------------

# A commit SHA: 7-40 hex chars, optionally prefixed with the word "commit".
_SHA_REF_SRC = r"(?:commit\s+)?(?P<sha>[0-9a-fA-F]{7,40})\b"

# A PR reference boundary fragment. Structurally mirrors ``ISSUE_REF_PATTERN``
# (optional ``owner/repo`` then ``#<number>``); the authoritative parse of
# owner/repo/number reuses ``ISSUE_REF_PATTERN`` itself in ``_parse_pr_ref``.
_PR_REF_SRC = r"(?:[\w.-]+/[\w.-]+)?#\d+"

# Matches a backport/cherry-pick cue immediately followed by a reference. The
# reference must directly follow the cue (only "of"/"from"/"for" connectors and
# punctuation/whitespace between) so passing mentions like "we should backport
# this someday, see #99" do not trigger a fetch.
_BACKPORT_PATTERN = re.compile(
    r"(?P<kind>back[\s-]?port|cherry[\s-]?pick)(?:ed|s|ing)?"
    r"(?:\s+(?:of|from|for))?"
    r"\s*:?\s*"
    r"(?P<ref>" + _SHA_REF_SRC + r"|" + _PR_REF_SRC + r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BackportReference:
    """A single backport/cherry-pick source reference parsed from PR text."""

    kind: str  # "pr" | "sha"
    owner: str
    repo: str
    number: int | None = None
    sha: str = ""
    cross_repo: bool = False
    raw: str = ""

    def describe(self) -> str:
        """Human-friendly identifier used in finding text."""
        if self.kind == "pr":
            base = f"#{self.number}"
            return f"{self.owner}/{self.repo}{base}" if self.cross_repo else base
        return f"commit {self.sha[:12]}"


def _parse_pr_ref(ref_text: str, default_owner: str, default_repo: str) -> BackportReference | None:
    """Parse a ``#123`` / ``org/repo#123`` reference, reusing ISSUE_REF_PATTERN."""
    match = ISSUE_REF_PATTERN.search(ref_text)
    if match is None:
        return None
    ref_owner, ref_repo, number_str = match.groups()
    owner = ref_owner or default_owner
    repo = ref_repo or default_repo
    cross_repo = bool(ref_owner) and (ref_owner != default_owner or ref_repo != default_repo)
    return BackportReference(
        kind="pr",
        owner=owner,
        repo=repo,
        number=int(number_str),
        cross_repo=cross_repo,
        raw=ref_text,
    )


def detect_backport_references(
    text: str, default_owner: str, default_repo: str
) -> list[BackportReference]:
    """Find all backport/cherry-pick source references in ``text``.

    Recognizes ``backport of #123``, ``cherry-pick of #123``, ``backport from
    #123``, cross-repo ``org/repo#123`` forms, and commit-SHA cherry-picks
    (``cherry-pick of <7-40 hex>``). Returns references in first-seen order,
    de-duplicated. The first entry is the primary reference.
    """
    refs: list[BackportReference] = []
    seen: set[tuple[str, str, str, str]] = set()

    for match in _BACKPORT_PATTERN.finditer(text or ""):
        sha = match.group("sha")
        if sha:
            ref: BackportReference | None = BackportReference(
                kind="sha",
                owner=default_owner,
                repo=default_repo,
                sha=sha,
                raw=match.group("ref"),
            )
        else:
            ref = _parse_pr_ref(match.group("ref"), default_owner, default_repo)
        if ref is None:
            continue

        key = (ref.kind, ref.owner, ref.repo, ref.sha.lower() or str(ref.number))
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)

    return refs


# --- diff-of-diffs comparison ----------------------------------------------


@dataclass(frozen=True)
class _FileChange:
    """Normalized changed-line content for a single file in a diff."""

    added: frozenset[str] = field(default_factory=frozenset)
    removed: frozenset[str] = field(default_factory=frozenset)


def _normalize(line: str) -> str:
    """Collapse whitespace so reindentation / trailing-space differences are
    treated as cosmetic and don't produce false positives."""
    return " ".join(line.split())


def _changed_lines_by_file(diff: str) -> dict[str, _FileChange]:
    """Parse a unified diff into ``{path: _FileChange}`` of normalized lines."""
    result: dict[str, _FileChange] = {}
    try:
        patch = PatchSet(diff)
    except Exception:
        logger.debug("backport: could not parse a diff for comparison", exc_info=True)
        return result

    for patched_file in patch:
        added: set[str] = set()
        removed: set[str] = set()
        for hunk in patched_file:
            for line in hunk:
                norm = _normalize(line.value)
                if not norm:
                    continue
                if line.is_added:
                    added.add(norm)
                elif line.is_removed:
                    removed.add(norm)
        result[patched_file.path] = _FileChange(added=frozenset(added), removed=frozenset(removed))
    return result


def _is_ignored(path: str, patterns: list[str]) -> bool:
    """Return True if ``path`` matches any ignore glob (file or dir prefix)."""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if fnmatch.fnmatch(path, pattern.rstrip("/") + "/*"):
            return True
    return False


def compare_diffs(
    source_diff: str,
    backport_diff: str,
    *,
    ignore_paths: list[str],
    config: BackportConfig,
) -> list[ReviewFinding]:
    """Compare a source diff against a backport diff, returning divergences.

    Produces findings for (a) files changed in the source but missing from the
    backport, (b) extra files changed only in the backport, and (c) shared
    files whose changed lines differ. The comparison uses normalized
    changed-line *sets*, so it is robust to hunk reordering and cosmetic
    whitespace-only differences.
    """
    source = _changed_lines_by_file(source_diff)
    backport = _changed_lines_by_file(backport_diff)

    source_files = {p for p in source if not _is_ignored(p, ignore_paths)}
    backport_files = {p for p in backport if not _is_ignored(p, ignore_paths)}

    findings: list[ReviewFinding] = []

    # (a) files changed in the source but not touched by the backport.
    if config.warn_on_missing_hunks:
        for path in sorted(source_files - backport_files):
            findings.append(
                ReviewFinding(
                    file_path=path,
                    title=f"backport: {path} missing from backport",
                    body=(
                        f"The source changes `{path}` but the backport does not touch it. "
                        "Confirm the omission is intentional (e.g. the file does not exist "
                        "on this branch) rather than a dropped change."
                    ),
                    confidence=0.7,
                    severity="important",
                )
            )

    # (b) files changed only in the backport.
    if config.warn_on_extra_files:
        for path in sorted(backport_files - source_files):
            findings.append(
                ReviewFinding(
                    file_path=path,
                    title=f"backport: {path} not changed in source",
                    body=(
                        f"The backport changes `{path}` but the source does not. "
                        "Confirm this extra change belongs in the backport."
                    ),
                    confidence=0.6,
                    severity="nit",
                )
            )

    # (c) files present in both whose changed lines differ.
    for path in sorted(source_files & backport_files):
        src = source[path]
        bp = backport[path]
        missing = (src.added - bp.added) | (src.removed - bp.removed)
        extra = (bp.added - src.added) | (bp.removed - src.removed)

        report_missing = bool(missing) and config.warn_on_missing_hunks
        report_extra = bool(extra) and config.warn_on_altered_hunks
        if not (report_missing or report_extra):
            continue

        parts: list[str] = []
        if report_missing:
            parts.append(
                f"{len(missing)} changed line(s) present in the source are missing "
                "from the backport"
            )
        if report_extra:
            parts.append(
                f"{len(extra)} changed line(s) in the backport are not present in the source"
            )
        findings.append(
            ReviewFinding(
                file_path=path,
                title=f"backport: {path} differs from source",
                body=(
                    f"`{path}` differs between the source and the backport: "
                    + "; ".join(parts)
                    + ". Eyeball the two diffs to confirm the backport faithfully "
                    "mirrors the original."
                ),
                confidence=0.65,
                severity="important" if report_missing else "nit",
            )
        )

    return findings


# --- the check -------------------------------------------------------------


class BackportSourceError(Exception):
    """Raised when the declared backport source diff cannot be fetched."""


class BackportCheck(BaseCheck):
    """Flags differences between a declared backport PR and its source diff.

    Deterministic (non-LLM) ``scan()`` is the primary path. The optional
    LLM semantic-drift layer is off by default (see ``BackportConfig``).
    """

    name = "backport"

    def __init__(
        self,
        config: BackportConfig | None = None,
        *,
        forge_client: ForgeClient | None = None,
    ) -> None:
        from franktheunicorn.config.models import BackportConfig as _Cfg

        self._config = config if config is not None else _Cfg()
        self._forge_client = forge_client

    def build_prompt(self, diff: str, pr_context: PRContext) -> tuple[str, str]:
        # This check uses scan(); build_prompt is a stub so BaseCheck stays
        # satisfied and the prompt-dispatch fallback has something to call.
        del diff, pr_context
        return ("", "")

    def scan(
        self,
        pr: PullRequest,
        diff: str,
        backend_config: LLMBackendConfig,
    ) -> list[ReviewFinding]:
        del backend_config  # deterministic path needs no LLM backend

        if not self._config.enabled:
            return []

        owner = pr.project.owner
        repo = pr.project.repo
        text = f"{pr.title or ''}\n{pr.body or ''}"

        refs = detect_backport_references(text, owner, repo)
        if not refs:
            # Not a declared backport — silent no-op.
            return []

        primary = refs[0]
        try:
            source_diff = self._fetch_source_diff(primary)
        except Exception as exc:
            logger.debug(
                "backport: could not fetch source %s for PR #%d",
                primary.describe(),
                pr.number,
                exc_info=True,
            )
            return [self._fetch_failure_finding(primary, _reason(exc))]

        if not source_diff.strip():
            return [self._fetch_failure_finding(primary, "the source diff was empty")]

        findings = compare_diffs(
            source_diff,
            diff or "",
            ignore_paths=self._config.ignore_paths,
            config=self._config,
        )

        if len(refs) > 1:
            findings.append(self._multiple_refs_note(refs))

        # Optional LLM semantic-drift layer (off by default). Kept as a clear
        # extension point rather than implemented, to keep this PR tight.
        # TODO(v1.5): when self._config.llm_semantic_drift is True, feed both
        # diffs to the LLM/agent-cli path (reusing review.prompt helpers) for a
        # semantic-similarity note, and append it here.

        return findings

    def _fetch_source_diff(self, ref: BackportReference) -> str:
        if self._forge_client is None:
            raise BackportSourceError("no forge client was available to fetch the source diff")
        if ref.kind == "pr":
            assert ref.number is not None
            return self._forge_client.get_pull_request_diff(ref.owner, ref.repo, ref.number)
        return self._forge_client.get_commit_diff(ref.owner, ref.repo, ref.sha)

    @staticmethod
    def _fetch_failure_finding(ref: BackportReference, reason: str) -> ReviewFinding:
        return ReviewFinding(
            file_path="",
            line_number=None,
            title="backport: source could not be verified",
            body=(
                f"This PR declares a backport of {ref.describe()} but the source "
                f"could not be fetched to verify it faithfully mirrors the original "
                f"({reason})."
            ),
            confidence=0.5,
            severity="informational",
        )

    @staticmethod
    def _multiple_refs_note(refs: list[BackportReference]) -> ReviewFinding:
        others = ", ".join(r.describe() for r in refs[1:])
        return ReviewFinding(
            file_path="",
            line_number=None,
            title="backport: multiple source references",
            body=(
                f"This PR references multiple backport sources; only the first "
                f"({refs[0].describe()}) was compared. Also referenced: {others}."
            ),
            confidence=0.5,
            severity="informational",
        )


def _reason(exc: Exception) -> str:
    """Compact, human-readable reason string for a fetch failure."""
    text = str(exc).strip()
    return text if text else type(exc).__name__
