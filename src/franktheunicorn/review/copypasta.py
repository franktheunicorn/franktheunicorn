"""
Copy-pasta detection for pull request reviews.

Detects when newly introduced code duplicates existing code in the repository.
Uses two complementary approaches:

- **Tier 1a (symilar)**: pylint's line-based duplicate checker with
  whitespace/comment normalization. Fast, catches exact-ish copies.
- **Tier 1b (winnowing)**: copydetect's fingerprinting algorithm (same as
  Stanford MOSS). Catches restructured, interleaved, and variable-renamed copies.
- **Tier 2 (LLM)**: Semantic similarity via LLM. Stubbed for now.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from copydetect.detector import CodeFingerprint, compare_files  # type: ignore[import-untyped]
from pylint.checkers.symilar import Symilar
from unidiff import PatchSet  # type: ignore[import-untyped]

from franktheunicorn.core.models import ReviewDraft

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from franktheunicorn.config.models import ProjectConfig
    from franktheunicorn.core.models import PullRequest
    from franktheunicorn.data_access.github.types import PRDiff

logger = logging.getLogger(__name__)

# Prefix used to identify PR chunk virtual files in Symilar results
_PR_CHUNK_PREFIX = "pr_chunk:"

# Default winnowing parameters for copydetect
_WINNOW_K = 25  # k-gram size
_WINNOW_WIN = 4  # window size

# Minimum overlap ratio from copydetect to count as a match
_WINNOW_MIN_OVERLAP = 0.5

# Scaffolding that is duplicated on purpose in every file that needs it.
# Matched against whitespace-collapsed lines, so indentation doesn't matter.
# Deliberately conservative: only lines that carry no logic of their own.
_BOILERPLATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Comments and license headers (Python/shell, C-family, SQL, block bodies)
    re.compile(r"^(#|//|/\*|\*/|\*(\s|$)|--)"),
    # Imports / package declarations
    re.compile(r"^(import|from\s+\S+\s+import|package|using)\b"),
    # Decorators and annotations
    re.compile(r"^@"),
    # Test-runner entry points
    re.compile(r"^if\s+__name__\s*==\s*['\"]__main__['\"]\s*:$"),
    re.compile(r"^(sys\.exit\()?((unittest|pytest|absltest)\.)?main\([^)]*\)\)?;?$"),
    re.compile(r"^(public\s+|static\s+)*void\s+main\s*\(.*$"),
    # Bare control-flow keywords and block punctuation
    re.compile(r"^(pass|break|continue|return|end|else|try|finally|do)\s*:?\s*\{?$"),
    re.compile(r"^[\[\](){};,:]+$"),
)

# One alternation over the above. The ubiquity check normalizes every line of
# every scanned repo file, so a single pass per line beats one per pattern.
# Safe to combine: each alternative is independently anchored.
_BOILERPLATE_RE = re.compile("|".join(f"(?:{p.pattern})" for p in _BOILERPLATE_PATTERNS))

# The ubiquity rule ("already in more than N files, so it's an idiom") only
# holds for scaffolding-sized blocks. Above this many meaningful lines —
# expressed as a multiple of copypasta_min_lines — being duplicated widely is
# the finding, not a reason to stay quiet.
_UBIQUITY_MAX_BLOCK_FACTOR = 2


@dataclass(frozen=True)
class CodeChunk:
    """A contiguous block of added code from a PR diff."""

    file_path: str
    start_line: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class CopyPastaMatch:
    """A detected duplication between new and existing code."""

    source_file: str
    source_start_line: int
    source_end_line: int
    new_file: str
    # Where the finding is *anchored* — the line the review comment lands on,
    # and the dedup key in _create_drafts. The head of the added block, not
    # necessarily the first duplicated line within it.
    new_start_line: int
    num_lines: int
    tier: str  # "symilar", "winnowing", or "llm"
    # The duplicated lines as they appear in the PR. Used by the noise filter
    # to tell a real copy-paste from scaffolding every file already has.
    matched_lines: tuple[str, ...] = field(default_factory=tuple)
    # NOTE: there is deliberately no per-match line range here. ReviewDraft has a
    # line_end field (five posters consume it, nothing produces it) and wiring it
    # up would be the natural home for one — but neither tier can supply an
    # honest range: _slice_line_range returns the hull across disjoint matched
    # regions, and symilar's count is of stripped common lines, not a span.
    # Anchoring on new_start_line and describing the extent in prose is the
    # strongest claim the data supports.


def check_copypasta(
    pr: PullRequest,
    diff: PRDiff,
    project_config: ProjectConfig,
    repo_path: Path,
) -> list[ReviewDraft]:
    """Run copy-pasta detection and return ReviewDraft findings.

    Returns an empty list if ``copypasta_enabled`` is False or if no
    duplications are detected.
    """
    if not project_config.copypasta_enabled:
        return []

    extra_patterns = _compile_ignore_patterns(project_config.copypasta_ignore_patterns)
    min_lines = project_config.copypasta_min_lines

    chunks = extract_added_chunks(diff, min_lines=min_lines)
    # Only compare chunks in languages we actually scan. Without this, a PR
    # adding a golden .csv or a .md file gets fingerprinted against the repo
    # (copydetect then warns "not tokenized: unknown file extension") for a
    # comparison that can never be meaningful.
    chunks = [
        c
        for c in chunks
        if any(c.file_path.endswith(ext) for ext in project_config.copypasta_scan_extensions)
    ]
    # Drop chunks that are pure scaffolding before spending any matcher time
    # on them.
    chunks = [c for c in chunks if len(_meaningful_lines(c.lines, extra_patterns)) >= min_lines]
    if not chunks:
        return []

    # Exclude files being modified in this PR to avoid self-matches
    pr_changed_files = {fc.filename for fc in diff.files}

    repo_files = _read_repo_files(
        repo_path,
        extensions=project_config.copypasta_scan_extensions,
        ignore_paths=project_config.ignore_paths,
        exclude_files=pr_changed_files,
    )
    if not repo_files:
        logger.debug("No repo files found to scan for copy-pasta")
        return []

    # Tier 1a: pylint symilar (line-based)
    matches = _check_symilar(chunks, repo_files, min_lines)

    # Track which chunks already matched so tier 1b skips them
    matched_chunk_keys = {(m.new_file, m.new_start_line) for m in matches}

    # Tier 1b: copydetect winnowing (fingerprint-based)
    unmatched_chunks = [c for c in chunks if (c.file_path, c.start_line) not in matched_chunk_keys]
    if unmatched_chunks:
        winnow_matches = _check_winnowing(unmatched_chunks, repo_files)
        matches.extend(winnow_matches)

    # Tier 2: LLM (stub)
    if project_config.copypasta_llm_enabled:
        all_matched = {(m.new_file, m.new_start_line) for m in matches}
        llm_chunks = [c for c in chunks if (c.file_path, c.start_line) not in all_matched]
        if llm_chunks:
            llm_matches = _check_llm(llm_chunks, repo_files)
            matches.extend(llm_matches)

    matches = _suppress_noise(
        matches,
        repo_files,
        min_lines=min_lines,
        max_repo_occurrences=project_config.copypasta_max_repo_occurrences,
        extra_patterns=extra_patterns,
    )

    return _create_drafts(pr, matches)


def _compile_ignore_patterns(patterns: Sequence[str]) -> tuple[re.Pattern[str], ...]:
    """Compile per-project boilerplate regexes, skipping any that don't compile.

    ``ProjectConfig`` validates these at load time; a bad pattern reaching
    here means the config was built in code, and one typo shouldn't take the
    whole review cycle down with it.
    """
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            logger.warning("Ignoring invalid copypasta_ignore_patterns entry %r", pattern)
    return tuple(compiled)


def _normalize_line(line: str) -> str:
    """Collapse whitespace so indentation and alignment don't affect matching."""
    return " ".join(line.split())


def _meaningful_lines(
    lines: Iterable[str],
    extra_patterns: Sequence[re.Pattern[str]] = (),
) -> list[str]:
    """Normalize *lines*, dropping blanks and boilerplate scaffolding.

    What's left is the part of a block that would actually be worth
    extracting into a shared helper.
    """
    result: list[str] = []
    for raw in lines:
        norm = _normalize_line(raw)
        if not norm:
            continue
        if _BOILERPLATE_RE.search(norm):
            continue
        if any(p.search(norm) for p in extra_patterns):
            continue
        result.append(norm)
    return result


def _suppress_noise(
    matches: list[CopyPastaMatch],
    repo_files: dict[str, str],
    *,
    min_lines: int,
    max_repo_occurrences: int,
    extra_patterns: Sequence[re.Pattern[str]] = (),
) -> list[CopyPastaMatch]:
    """Drop matches that are scaffolding rather than copy-paste.

    Two rules, both aimed at the same failure mode — every test file in a
    project carries the same main guard, license header, and harness call, and
    flagging that is noise the operator has to reject one draft at a time:

    1. **Boilerplate**: after stripping comments, imports, main guards, and
       bare block punctuation, a block with fewer than ``min_lines`` of real
       code left isn't worth a comment.
    2. **Ubiquity**: a block that already appears in more than
       ``max_repo_occurrences`` files in the repo is an established idiom.
       ``0`` disables this check.
    """
    # (match, block_id) pairs — block_id is None when the ubiquity check
    # doesn't apply to that match.
    kept: list[tuple[CopyPastaMatch, int | None]] = []
    blocks: dict[int, tuple[str, ...]] = {}

    for match in matches:
        # Matches from older callers may carry no line content; the ubiquity
        # check can't run on them, so let them through as before.
        if not match.matched_lines:
            kept.append((match, None))
            continue

        meaningful = _meaningful_lines(match.matched_lines, extra_patterns)
        if len(meaningful) < min_lines:
            logger.debug(
                "copypasta: dropping boilerplate match at %s:%d (%d meaningful line(s))",
                match.new_file,
                match.new_start_line,
                len(meaningful),
            )
            continue

        # "Lives in lots of files" only implies "is scaffolding" for small
        # blocks. A 200-line block copy-pasted into a fourth file is the
        # strongest copy-paste signal there is, and suppressing it would be
        # backwards — so the ubiquity rule only applies below a size ceiling.
        if max_repo_occurrences <= 0 or len(meaningful) > _UBIQUITY_MAX_BLOCK_FACTOR * min_lines:
            kept.append((match, None))
            continue

        block_id = len(blocks)
        blocks[block_id] = tuple(meaningful)
        kept.append((match, block_id))

    if not blocks:
        return [match for match, _ in kept]

    counts = _count_files_containing(blocks, repo_files, extra_patterns)
    result: list[CopyPastaMatch] = []
    for match, kept_block_id in kept:
        occurrences = counts.get(kept_block_id, 0) if kept_block_id is not None else 0
        if kept_block_id is not None and occurrences > max_repo_occurrences:
            logger.info(
                "copypasta: dropping ubiquitous block at %s:%d — already in %d repo file(s)",
                match.new_file,
                match.new_start_line,
                occurrences,
            )
            continue
        result.append(match)
    return result


def _count_files_containing(
    blocks: dict[int, tuple[str, ...]],
    repo_files: dict[str, str],
    extra_patterns: Sequence[re.Pattern[str]] = (),
) -> dict[int, int]:
    """Count, per block, how many repo files contain it as a run of code lines.

    One pass over the repo: each file's meaningful lines are computed once and
    every block anchored on its first line is checked against that position.
    """
    anchors: dict[str, list[int]] = defaultdict(list)
    for block_id, block in blocks.items():
        anchors[block[0]].append(block_id)

    counts: Counter[int] = Counter()
    for content in repo_files.values():
        file_lines = _meaningful_lines(content.splitlines(), extra_patterns)
        seen_here: set[int] = set()
        for position, line in enumerate(file_lines):
            for block_id in anchors.get(line, ()):
                if block_id in seen_here:
                    continue
                block = blocks[block_id]
                if file_lines[position : position + len(block)] == list(block):
                    seen_here.add(block_id)
        for block_id in seen_here:
            counts[block_id] += 1

    return dict(counts)


def extract_added_chunks(diff: PRDiff, min_lines: int = 4) -> list[CodeChunk]:
    """Extract contiguous blocks of added lines from a PR diff.

    Uses the ``unidiff`` library to parse unified diff patches rather than
    hand-rolling a parser.
    """
    chunks: list[CodeChunk] = []

    for file_change in diff.files:
        if not file_change.patch or file_change.status == "removed":
            continue

        # unidiff needs diff headers; synthesise a minimal one from the patch
        full_diff = (
            f"--- a/{file_change.filename}\n+++ b/{file_change.filename}\n{file_change.patch}\n"
        )
        try:
            patch_set = PatchSet(full_diff)
        except Exception:
            logger.debug("Could not parse patch for %s, skipping", file_change.filename)
            continue

        for patched_file in patch_set:
            for hunk in patched_file:
                current_lines: list[str] = []
                current_start: int = 0

                for line in hunk:
                    if line.is_added:
                        if not current_lines:
                            current_start = line.target_line_no
                        current_lines.append(line.value.rstrip("\n"))
                    else:
                        # Context or removal — flush any accumulated chunk
                        if len(current_lines) >= min_lines:
                            chunks.append(
                                CodeChunk(
                                    file_path=file_change.filename,
                                    start_line=current_start,
                                    lines=tuple(current_lines),
                                )
                            )
                        current_lines = []

                # Flush final chunk in hunk
                if len(current_lines) >= min_lines:
                    chunks.append(
                        CodeChunk(
                            file_path=file_change.filename,
                            start_line=current_start,
                            lines=tuple(current_lines),
                        )
                    )

    return chunks


def _check_symilar(
    chunks: list[CodeChunk],
    repo_files: dict[str, str],
    min_lines: int,
) -> list[CopyPastaMatch]:
    """Tier 1a: Use pylint's Symilar for line-based duplicate detection."""
    matches: list[CopyPastaMatch] = []

    sym = Symilar(
        min_lines=min_lines,
        ignore_comments=True,
        ignore_docstrings=True,
        ignore_imports=True,
    )

    # Add PR chunks as virtual files
    chunk_names: set[str] = set()
    chunks_by_name: dict[str, CodeChunk] = {}
    for chunk in chunks:
        name = f"{_PR_CHUNK_PREFIX}{chunk.file_path}:{chunk.start_line}"
        chunk_names.add(name)
        chunks_by_name[name] = chunk
        try:
            sym.append_stream(name, StringIO("\n".join(chunk.lines) + "\n"))
        except Exception:
            # Symilar parses content as Python AST; non-Python or syntax-broken
            # chunks (e.g. YAML, docstrings with unmatched quotes) raise
            # AstroidSyntaxError.  Skip them here — the caller will route them
            # to the winnowing tier instead.
            logger.debug(
                "symilar: skipping unparseable chunk %s (will fall back to winnowing)",
                name,
            )

    # Add existing repo files; skip any that also fail AST parsing.
    for path, content in repo_files.items():
        try:
            sym.append_stream(path, StringIO(content))
        except Exception:
            logger.debug("symilar: skipping unparseable repo file %s", path)

    # NOTE: deliberately *not* calling sym.run(). run() compares every pair of
    # appended linesets — quadratic in file count, and all but a handful of
    # those pairs are repo-vs-repo, which has nothing to do with this PR — then
    # prints the whole report to stdout. Measured over 400 files of Spark's
    # python/ tree: run() took 68s and printed 262KB of report, against 0.42s
    # for one chunk / 3.7s for twenty in the loop below.
    #
    # That loop is linear in chunks x repo files rather than constant, because
    # _find_common re-hashes both linesets on every call and there's no public
    # way to hand it a precomputed hash. Worth revisiting if PRs with many
    # separate added blocks start dominating cycle time.

    # Find commonalities between PR chunks and repo files.
    # NOTE: _find_common is a private API on Symilar. There is no public
    # interface to retrieve structured match data. Pin pylint tightly if
    # this breaks across versions.
    # Only PR-chunk-vs-repo-file pairs matter, so walk those directly instead
    # of every pair of linesets.
    pr_linesets = [ls for ls in sym.linesets if ls.name in chunk_names]
    repo_linesets = [ls for ls in sym.linesets if ls.name not in chunk_names]

    for pr_ls in pr_linesets:
        # Extract original chunk info from the virtual file name
        chunk_info = pr_ls.name.removeprefix(_PR_CHUNK_PREFIX)
        chunk_file, chunk_line_str = chunk_info.rsplit(":", 1)
        chunk_base_line = int(chunk_line_str)
        chunk_lines = chunks_by_name[pr_ls.name].lines

        for repo_ls in repo_linesets:
            # _find_common(a, b) always yields fst_lset=a (checked against
            # pylint 4.0.6), so fst_* is the PR side and snd_* the repo side.
            for common in sym._find_common(pr_ls, repo_ls):
                matches.append(
                    CopyPastaMatch(
                        source_file=common.snd_lset.name,
                        source_start_line=common.snd_file_start + 1,  # 0- to 1-indexed
                        source_end_line=common.snd_file_end,
                        new_file=chunk_file,
                        new_start_line=chunk_base_line + common.fst_file_start,
                        num_lines=common.cmn_lines_nb,
                        tier="symilar",
                        # Symilar's line indices address the virtual file,
                        # whose content is exactly the chunk's lines.
                        matched_lines=tuple(
                            chunk_lines[common.fst_file_start : common.fst_file_end]
                        ),
                    )
                )

    return matches


def _check_winnowing(
    chunks: list[CodeChunk],
    repo_files: dict[str, str],
) -> list[CopyPastaMatch]:
    """Tier 1b: Use copydetect's winnowing for fingerprint-based detection."""
    matches: list[CopyPastaMatch] = []

    # Pre-compute fingerprints for repo files
    repo_fps: dict[str, CodeFingerprint] = {}
    for path, content in repo_files.items():
        if not content.strip():
            continue
        try:
            repo_fps[path] = CodeFingerprint(
                file=path,
                k=_WINNOW_K,
                win_size=_WINNOW_WIN,
                fp=StringIO(content),
            )
        except Exception:
            logger.debug("Could not fingerprint %s, skipping", path)

    for chunk in chunks:
        chunk_text = "\n".join(chunk.lines) + "\n"
        if len(chunk_text.strip()) < _WINNOW_K:
            continue

        try:
            chunk_fp = CodeFingerprint(
                file=chunk.file_path,
                k=_WINNOW_K,
                win_size=_WINNOW_WIN,
                fp=StringIO(chunk_text),
            )
        except Exception:
            logger.debug("Could not fingerprint chunk %s:%d", chunk.file_path, chunk.start_line)
            continue

        for path, repo_fp in repo_fps.items():
            try:
                overlap_tokens, (ratio1, _ratio2), (_chunk_slices, repo_slices) = compare_files(
                    chunk_fp, repo_fp
                )
            except Exception:
                continue

            if overlap_tokens > 0 and ratio1 >= _WINNOW_MIN_OVERLAP:
                source_range = _slice_line_range(getattr(repo_fp, "raw_code", ""), repo_slices) or (
                    1,
                    1,
                )

                # Narrow to the region that actually overlapped. The threshold
                # is a ratio, so "matched" is typically part of the chunk, not
                # all of it — and handing the whole chunk to the noise filters
                # means the ubiquity check looks for a block that exists
                # nowhere verbatim and therefore never fires.
                new_range = _slice_line_range(getattr(chunk_fp, "raw_code", ""), _chunk_slices)
                matched_lines = chunk.lines
                if new_range is not None:
                    narrowed = chunk.lines[new_range[0] - 1 : new_range[1]]
                    if narrowed:  # slices outside the chunk: keep the whole one
                        matched_lines = narrowed

                matches.append(
                    CopyPastaMatch(
                        source_file=path,
                        source_start_line=source_range[0],
                        source_end_line=source_range[1],
                        new_file=chunk.file_path,
                        # Anchored at the chunk, deliberately: _create_drafts
                        # dedups on (new_file, new_start_line), so varying this
                        # per matched repo file turns one duplicated block into
                        # one draft per file it resembles.
                        new_start_line=chunk.start_line,
                        num_lines=len(matched_lines),
                        tier="winnowing",
                        matched_lines=matched_lines,
                    )
                )

    return matches


def _slice_line_range(raw_code: str, slices: object) -> tuple[int, int] | None:
    """Convert copydetect's character-offset slices to a 1-based line range.

    Returns None when the slices aren't usable, so callers can fall back
    instead of reporting line 1.
    """
    if not raw_code or not hasattr(slices, "__len__") or len(slices) == 0:
        return None
    try:
        start = raw_code[: int(slices[0][0])].count("\n") + 1  # type: ignore[index]
        end = raw_code[: int(slices[1][-1])].count("\n") + 1  # type: ignore[index]
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    return start, max(end, start)


def _check_llm(
    chunks: list[CodeChunk],
    repo_files: dict[str, str],
) -> list[CopyPastaMatch]:
    """Tier 2: LLM-powered semantic similarity detection (stub).

    This is a placeholder. The real implementation will use an LLM to
    detect semantic duplication that escapes syntactic matchers. The
    interface is ready — swap in the real provider when LLM integration
    lands in the review pipeline.
    """
    return []


def _read_repo_files(
    repo_path: Path,
    extensions: list[str],
    ignore_paths: list[str],
    exclude_files: set[str] | None = None,
) -> dict[str, str]:
    """Read tracked files from the repository, filtered by extension and ignore paths."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        logger.warning("Could not list files in %s", repo_path)
        return {}

    files: dict[str, str] = {}
    for rel_path in result.stdout.strip().splitlines():
        if not rel_path:
            continue

        # Filter by extension
        if not any(rel_path.endswith(ext) for ext in extensions):
            continue

        # Respect ignore_paths
        if any(rel_path.startswith(ip) for ip in ignore_paths):
            continue

        # Skip files being modified in the PR (avoid self-matches)
        if exclude_files and rel_path in exclude_files:
            continue

        full_path = repo_path / rel_path
        try:
            files[rel_path] = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.debug("Could not read %s", full_path)

    return files


def _create_drafts(
    pr: PullRequest,
    matches: list[CopyPastaMatch],
) -> list[ReviewDraft]:
    """Convert copy-pasta matches to ReviewDraft objects.

    Gates each match through the anti-pattern list (the core learning loop
    applies to every finding generator) and uses ``get_or_create`` so the
    worker re-scanning an unchanged PR every poll cycle doesn't pile up
    duplicate drafts.
    """
    from franktheunicorn.review.antipattern import (
        check_against_anti_patterns,
        record_anti_pattern_matches,
    )

    # Deduplicate by (new_file, new_start_line) — keep first match
    seen: set[tuple[str, int]] = set()
    drafts: list[ReviewDraft] = []

    for match in matches:
        key = (match.new_file, match.new_start_line)
        if key in seen:
            continue
        seen.add(key)

        confidence = 0.85 if match.tier in ("symilar", "winnowing") else 0.65
        tier_label = {
            "symilar": "line-level",
            "winnowing": "structural",
            "llm": "semantic",
        }.get(match.tier, match.tier)

        if match.source_start_line != match.source_end_line:
            location = (
                f"`{match.source_file}` "
                f"(lines {match.source_start_line}\u2013{match.source_end_line})"
            )
        else:
            location = f"`{match.source_file}`"

        # "spanning N lines", not "N lines" and not an explicit A\u2013B range.
        #
        # The comment is anchored on the head of the added block (that's the
        # dedup key) while num_lines measures only the matched part, so a bare
        # count read as "the N lines starting here" and pointed at the wrong
        # place. Naming the range instead is worse: for the winnowing tier it
        # comes from _slice_line_range, which spans first-region-start to
        # last-region-end across *disjoint* matches, so a block matching lines
        # 1-3 and 15-17 reports 1-17 and accuses the author of duplicating the
        # eleven lines in between. "Spanning" is the strongest claim both tiers
        # actually support.
        comment_body = (
            f"Possible copy-paste ({tier_label} match spanning {match.num_lines} lines): "
            f"this code appears to duplicate existing code in {location}. "
            f"Consider extracting a shared function or reusing the existing implementation."
        )

        ap_matches = check_against_anti_patterns(comment_body, pr.project)
        if ap_matches:
            record_anti_pattern_matches(ap_matches)
            logger.info(
                "Suppressed copypasta finding for %s:%d — matched anti-pattern(s)",
                match.new_file,
                match.new_start_line,
            )
            continue

        draft, _created = ReviewDraft.objects.get_or_create(
            pull_request=pr,
            file_path=match.new_file,
            line_number=match.new_start_line,
            backend_used="copypasta",
            defaults={
                "comment_body": comment_body,
                "confidence": confidence,
                "sources": ["copypasta"],
                "status": "pending",
            },
        )
        drafts.append(draft)

    return drafts
