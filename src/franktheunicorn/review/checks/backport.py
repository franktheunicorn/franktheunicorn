"""Backport / cherry-pick faithfulness sub-check.

When a PR declares itself a backport or cherry-pick of another PR or commit,
this check fetches the ORIGINAL (source) diff, compares it against the
backport PR's diff, and flags SEEMING DIFFERENCES so a human can eyeball
whether the backport faithfully mirrors the original. When the PR is not a
declared backport the check is a silent no-op (no findings).

It is a deterministic, non-LLM check: detection is regex based and the
comparison is a diff-of-diffs over :class:`unidiff.PatchSet`.

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
from collections import Counter
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

# True if the PR text declares a backport/cherry-pick at all (cue anywhere).
_DECLARES_BACKPORT = re.compile(r"back[\s-]?port|cherry[\s-]?pick", re.IGNORECASE)

# A single source reference: a commit SHA (optionally qualified by
# "commit"/"sha") or a PR reference (``#123`` / ``org/repo#123``). The trailing
# ``\b`` anchors keep us from matching hex-adjacent substrings. A short,
# *unqualified* hex token (e.g. "deadbeef" in prose) is deliberately parsed
# out here but rejected in ``_ref_from_match`` — only full 40-char SHAs or
# ``commit <sha>``-qualified tokens count as commit references.
_REF_SRC = (
    r"(?:(?P<shaqual>commit|sha)\s+)?(?P<sha>[0-9a-fA-F]{7,40})\b"
    r"|"
    r"(?P<prref>(?:[\w.-]+/[\w.-]+)?#\d+)\b"
)

# Ref directly attached to a backport/cherry-pick cue: "backport of #123",
# "cherry-picked from abc…", "backport #123".
_KEYWORD_REF = re.compile(
    r"\b(?:back[\s-]?port|cherry[\s-]?pick)(?:ed|s|ing)?"
    r"(?:\s+(?:of|from|for))?\s*:?\s*"
    r"(?:" + _REF_SRC + r")",
    re.IGNORECASE | re.MULTILINE,
)

# Ref attached to a source-indicating connector anywhere in the text: "from
# #123", or a label-like "source: #123" / "Original: #123". Only consulted once
# the text is known to declare a backport.
#
# "of" is deliberately EXCLUDED here: it is far too common in prose ("part of
# #123", "because of #123", "a variation of #123") to be a reliable source
# signal, and the legitimate cue-adjacent form ("backport of #5") is already
# handled by _KEYWORD_REF. "source"/"original" are restricted to a label form
# (line start or immediately after punctuation) so mid-sentence prose like
# "restores the original #123 behavior" is not treated as a source.
_CONNECTOR_REF = re.compile(
    r"(?:"
    r"(?:^|[^\w/])from\s+"
    r"|"
    r"(?:^|[.;:,)\]}\-]\s*)(?:source|original|orig)\s*:?\s*"
    r")"
    r"(?:" + _REF_SRC + r")",
    re.IGNORECASE | re.MULTILINE,
)

# A unified diff always carries either a "diff --git" header or an "@@ -" hunk
# marker; an HTML interstitial / rate-limit page / login redirect carries
# neither. Used to reject non-diff 200 bodies before parsing.
_DIFF_MARKERS = ("diff --git", "@@ -")


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


def _ref_from_match(
    match: re.Match[str], default_owner: str, default_repo: str
) -> BackportReference | None:
    """Build a reference from a regex match, applying SHA-qualification rules."""
    groups = match.groupdict()
    prref = groups.get("prref")
    if prref:
        return _parse_pr_ref(prref, default_owner, default_repo)

    sha = groups.get("sha")
    if sha:
        qualified = bool(groups.get("shaqual"))
        # Only a full 40-char SHA or an explicitly qualified token is a commit
        # reference; a short unqualified hex token is prose, not a source.
        if qualified or len(sha) == 40:
            return BackportReference(
                kind="sha", owner=default_owner, repo=default_repo, sha=sha, raw=sha
            )
    return None


def detect_backport_references(
    text: str, default_owner: str, default_repo: str
) -> list[BackportReference]:
    """Find all backport/cherry-pick source references in ``text``.

    Detection is two-phase: (1) the text must *declare* a backport/cherry-pick
    somewhere, then (2) source references are gathered from cue-adjacent forms
    (``backport of #123``) and from source-indicating connectors anywhere in
    the text (``From #123``, ``Original: #123``, ``cherry-picked from <sha>``).
    A plain ``fixes #123`` / ``closes #123`` or a bare issue mention is never
    treated as a backport source.

    Recognizes same-repo ``#123``, cross-repo ``org/repo#123``, and commit-SHA
    references (full 40-char SHA, or a ``commit``/``sha``-qualified token).
    Returns references in first-seen order, de-duplicated; the first entry is
    the primary reference.
    """
    text = text or ""
    if not _DECLARES_BACKPORT.search(text):
        return []

    positioned: list[tuple[int, BackportReference]] = []
    for pattern in (_KEYWORD_REF, _CONNECTOR_REF):
        for match in pattern.finditer(text):
            ref = _ref_from_match(match, default_owner, default_repo)
            if ref is not None:
                # Order by where the reference token starts, not the cue.
                start = match.start("sha") if match.groupdict().get("sha") else match.start("prref")
                positioned.append((start, ref))

    positioned.sort(key=lambda pair: pair[0])

    refs: list[BackportReference] = []
    seen: set[tuple[str, str, str, str]] = set()
    for _start, ref in positioned:
        key = (ref.kind, ref.owner, ref.repo, ref.sha.lower() or str(ref.number))
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)

    return refs


# --- diff-of-diffs comparison ----------------------------------------------


@dataclass
class _FileChange:
    """Normalized changed-line content for a single file in a diff.

    Added/removed lines are stored as multisets (``Counter``) so a backport
    that drops or duplicates one of several identical changed lines does not
    compare equal to the source.
    """

    added: Counter[str] = field(default_factory=Counter)
    removed: Counter[str] = field(default_factory=Counter)


def _normalize(line: str) -> str:
    """Collapse whitespace so reindentation / trailing-space differences are
    treated as cosmetic and don't produce false positives."""
    return " ".join(line.split())


def _looks_like_diff(diff: str) -> bool:
    """True if ``diff`` carries a recognizable unified-diff marker."""
    return any(marker in diff for marker in _DIFF_MARKERS)


def _parse_changed_lines(diff: str) -> tuple[bool, dict[str, _FileChange]]:
    """Parse a unified diff into ``(parsed_ok, {path: _FileChange})``.

    ``parsed_ok`` distinguishes "nothing to compare" from "could not parse":

    - An empty/whitespace body → ``(True, {})`` (genuinely no changes).
    - A non-empty body that does not look like a diff, that ``PatchSet`` fails
      to parse, or that parses to zero files → ``(False, {})`` so the caller
      can surface a single "could not verify" finding instead of treating
      every source file as missing from the backport.
    """
    if not diff.strip():
        return True, {}
    if not _looks_like_diff(diff):
        return False, {}

    try:
        patch = PatchSet(diff)
    except Exception:
        logger.debug("backport: could not parse a fetched body as a diff", exc_info=True)
        return False, {}

    result: dict[str, _FileChange] = {}
    for patched_file in patch:
        added: Counter[str] = Counter()
        removed: Counter[str] = Counter()
        for hunk in patched_file:
            for line in hunk:
                norm = _normalize(line.value)
                if not norm:
                    continue
                if line.is_added:
                    added[norm] += 1
                elif line.is_removed:
                    removed[norm] += 1
        # Skip files with no content change (rename/mode-only, or a header with
        # no hunks — e.g. a truncated/non-diff body that only looked like one).
        if added or removed:
            result[patched_file.path] = _FileChange(added=added, removed=removed)

    if not result:
        # Looked like a diff but produced no changed files — treat as
        # unparseable so the caller surfaces one "could not verify" finding
        # rather than comparing against an empty source.
        return False, {}
    return True, result


def _is_ignored(path: str, patterns: list[str]) -> bool:
    """Return True if ``path`` matches any ignore glob (file or dir prefix)."""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if fnmatch.fnmatch(path, pattern.rstrip("/") + "/*"):
            return True
    return False


def _compare_maps(
    source: dict[str, _FileChange],
    backport: dict[str, _FileChange],
    *,
    ignore_paths: list[str],
    config: BackportConfig,
) -> list[ReviewFinding]:
    """Compare parsed source vs backport changed-line maps, returning findings."""
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

    # (c) files present in both whose changed lines differ (multiset diff).
    for path in sorted(source_files & backport_files):
        src = source[path]
        bp = backport[path]
        missing = (src.added - bp.added) + (src.removed - bp.removed)
        extra = (bp.added - src.added) + (bp.removed - src.removed)

        report_missing = bool(missing) and config.warn_on_missing_hunks
        report_extra = bool(extra) and config.warn_on_altered_hunks
        if not (report_missing or report_extra):
            continue

        parts: list[str] = []
        if report_missing:
            parts.append(
                f"{sum(missing.values())} changed line(s) present in the source are missing "
                "from the backport"
            )
        if report_extra:
            parts.append(
                f"{sum(extra.values())} changed line(s) in the backport are not present "
                "in the source"
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
    files whose changed lines differ. The comparison uses normalized changed-
    line *multisets*, so it is robust to hunk reordering and cosmetic
    whitespace-only differences while still catching dropped/duplicated lines.

    If either side is not parseable as a diff (e.g. an HTML interstitial), a
    single informational "could not verify" finding is returned rather than
    per-file spam — mirroring :meth:`BackportCheck.scan`'s contract.
    """
    source_ok, source = _parse_changed_lines(source_diff)
    backport_ok, backport = _parse_changed_lines(backport_diff)
    if not source_ok or not backport_ok:
        side = "source" if not source_ok else "backport"
        return [_unverifiable_finding(f"the {side} could not be parsed as a diff")]
    return _compare_maps(source, backport, ignore_paths=ignore_paths, config=config)


def _unverifiable_finding(detail: str) -> ReviewFinding:
    """A ref-agnostic informational finding for an unverifiable comparison."""
    return ReviewFinding(
        file_path="",
        line_number=None,
        title="backport: source could not be verified",
        body=(
            f"The backport could not be verified against its source: {detail} (could not verify)."
        ),
        confidence=0.5,
        severity="informational",
    )


# --- the check -------------------------------------------------------------


class BackportSourceError(Exception):
    """Raised when the declared backport source diff cannot be fetched."""


class BackportCheck(BaseCheck):
    """Flags differences between a declared backport PR and its source diff.

    Deterministic (non-LLM) ``scan()`` is the primary path. The optional
    LLM semantic-drift layer is a reserved, currently-unimplemented flag (see
    ``BackportConfig.llm_semantic_drift``).
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
        _backend_config: LLMBackendConfig,
    ) -> list[ReviewFinding]:
        if not self._config.enabled:
            return []

        owner = pr.project.owner
        repo = pr.project.repo
        text = f"{pr.title or ''}\n{pr.body or ''}"

        refs = detect_backport_references(text, owner, repo)
        if not refs:
            # Not a declared backport (or no resolvable source) — silent no-op.
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
            return [self._info_finding(primary, f"the source could not be fetched: {_reason(exc)}")]

        if len(source_diff) > self._config.max_source_diff_chars:
            return [
                self._info_finding(
                    primary,
                    f"the source diff is too large to verify ({len(source_diff)} bytes)",
                )
            ]

        if not source_diff.strip():
            return [self._info_finding(primary, "the source diff was empty")]

        source_ok, source_map = _parse_changed_lines(source_diff)
        if not source_ok:
            return [
                self._info_finding(
                    primary, "the fetched source could not be parsed as a diff (could not verify)"
                )
            ]

        backport_ok, backport_map = _parse_changed_lines(diff or "")
        if not backport_ok:
            return [
                self._info_finding(
                    primary, "the backport diff could not be parsed as a diff (could not verify)"
                )
            ]

        findings = _compare_maps(
            source_map,
            backport_map,
            ignore_paths=self._config.ignore_paths,
            config=self._config,
        )

        if len(refs) > 1:
            findings.append(self._multiple_refs_note(refs))

        # The optional LLM semantic-drift layer (config.llm_semantic_drift) is a
        # reserved flag: it is intentionally not implemented yet, so the
        # deterministic comparison above remains the only path.

        return findings

    def _fetch_source_diff(self, ref: BackportReference) -> str:
        if self._forge_client is None:
            raise BackportSourceError("no forge client was available to fetch the source diff")

        client_name = type(self._forge_client).__name__
        if ref.kind == "pr":
            assert ref.number is not None
            raw = self._forge_client.get_pull_request_diff(ref.owner, ref.repo, ref.number)
        else:
            try:
                raw = self._forge_client.get_commit_diff(ref.owner, ref.repo, ref.sha)
            except NotImplementedError as exc:
                raise BackportSourceError(
                    f"source forge {client_name} does not support fetching a commit diff"
                ) from exc

        if raw is None:
            raise BackportSourceError(
                f"source forge {client_name} does not support fetching the backport source diff"
            )
        return raw

    @staticmethod
    def _info_finding(ref: BackportReference, reason: str) -> ReviewFinding:
        return ReviewFinding(
            file_path="",
            line_number=None,
            title="backport: source could not be verified",
            body=(
                f"This PR declares a backport of {ref.describe()} but {reason}, so the "
                "backport could not be verified to faithfully mirror the original."
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


# Cap on how much of a raw error string ends up in a finding / DB row.
_REASON_MAX_CHARS = 200


def _reason(exc: Exception) -> str:
    """Compact, truncated, human-readable reason string for a fetch failure."""
    text = str(exc).strip() or type(exc).__name__
    if len(text) > _REASON_MAX_CHARS:
        text = text[:_REASON_MAX_CHARS].rstrip() + "…"
    return text
