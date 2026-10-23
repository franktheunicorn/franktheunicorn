"""Build optional full-file + first-party-import context for review prompts.

Reads files from the local checkout (the same one used for ``git blame``) and
formats them into two prompt sections, capped by the project's ``ContextConfig``
token budget. Tokens are approximated as ``len(text) // 4``.

The builder degrades gracefully: missing checkout, missing files, parse errors,
or disabled config all produce empty strings rather than exceptions.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from franktheunicorn.review.import_resolvers import get_resolver

logger = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Cheap token estimate. ~4 chars per token is a decent proxy for code."""
    return len(text) // 4


def build_context_strings(
    changed_files: Sequence[str],
    repo_path: Path | None,
    config: object,
    diff: str = "",
) -> tuple[str, str]:
    """Return ``(full_file_context, imported_modules_context)``.

    Both empty strings when the feature is disabled, the checkout is missing,
    or no eligible content fits the budget. ``config`` is duck-typed against
    ``ContextConfig`` to avoid importing config at module load.

    ``diff`` (the PR's unified diff) lets a file LARGER than ``per_file_cap``
    contribute an excerpt around its changed hunks instead of nothing — on a
    monorepo the interesting files are routinely 10x the cap, and skipping
    them whole left the prompt with no surrounding code at all exactly where
    it mattered most.
    """
    include_full = bool(getattr(config, "include_full_file", False))
    include_imports = bool(getattr(config, "include_first_party_imports", False))
    if not include_full and not include_imports:
        return "", ""
    if repo_path is None:
        return "", ""
    if not Path(repo_path).is_dir():
        return "", ""

    total_budget = int(getattr(config, "total_token_budget", 0))
    per_file_cap = int(getattr(config, "per_file_token_cap", 0))
    package_roots: list[str] = list(getattr(config, "package_roots", []) or [])
    if not package_roots:
        package_roots = _autodetect_package_roots(repo_path)

    hunk_ranges = _hunk_ranges_by_file(diff)
    remaining = total_budget
    repo_path = Path(repo_path)

    # Stage 1: read changed files, smallest first, into the full-file pool.
    # An over-cap file with known hunks becomes an excerpt around the change.
    candidates: list[tuple[str, str, int]] = []  # (rel_path, content, tokens)
    for rel in changed_files:
        if not rel:
            continue
        abs_path = repo_path / rel
        if not abs_path.is_file():
            continue
        content = _safe_read(abs_path)
        if content is None:
            continue
        tokens = estimate_tokens(content)
        if tokens > per_file_cap:
            ranges = hunk_ranges.get(rel)
            if not ranges:
                continue
            content = _excerpt_around(content, ranges, per_file_cap)
            if not content:
                continue
            tokens = estimate_tokens(content)
            if tokens > per_file_cap:
                continue
        candidates.append((rel, content, tokens))

    candidates.sort(key=lambda x: x[2])

    full_files: list[tuple[str, str]] = []
    chosen_paths: set[Path] = set()
    if include_full:
        for rel, content, tokens in candidates:
            if tokens > remaining:
                continue
            full_files.append((rel, content))
            chosen_paths.add((repo_path / rel).resolve())
            remaining -= tokens

    # Stage 2: resolve first-party imports of the chosen files, fill remaining budget.
    imported_files: list[tuple[str, str]] = []
    if include_imports and remaining > 0 and package_roots:
        seen_imports: set[Path] = set(chosen_paths)
        seed_rels = (
            [rel for rel, _ in full_files] if full_files else [rel for rel, _, _ in candidates]
        )
        for seed_rel in seed_rels:
            abs_path = repo_path / seed_rel
            resolver = get_resolver(abs_path)
            if resolver is None:
                continue
            try:
                imports = resolver.resolve(abs_path, repo_path, package_roots)
            except Exception:
                logger.debug("Resolver failed for %s", abs_path, exc_info=True)
                continue
            for imp_path in imports:
                resolved = imp_path.resolve()
                if resolved in seen_imports:
                    continue
                seen_imports.add(resolved)
                content = _safe_read(imp_path)
                if content is None:
                    continue
                tokens = estimate_tokens(content)
                if tokens > per_file_cap:
                    # No hunks to window around for an import; the head (doc,
                    # constants, the module's shape) is the useful part. On a
                    # monorepo the imports are big files too, and skipping
                    # them whole kept this section empty everywhere it mattered.
                    content = _head_truncated(content, per_file_cap)
                    if not content:
                        continue
                    tokens = estimate_tokens(content)
                    if tokens > per_file_cap:
                        continue
                if tokens > remaining:
                    continue
                try:
                    rel_imp = imp_path.resolve().relative_to(repo_path.resolve())
                except ValueError:
                    continue
                imported_files.append((str(rel_imp), content))
                remaining -= tokens

    return _render(full_files, "## Full file context"), _render(
        imported_files, "## Imported modules (first-party)"
    )


#: Lines of context on each side of a hunk when excerpting an over-cap file.
_EXCERPT_PAD_LINES = 40


def _head_truncated(content: str, token_cap: int) -> str:
    """The first ``token_cap`` tokens of a file, with a marker saying so.

    The marker is reserved against the cap so the joined result stays within
    ``token_cap * 4`` characters. Empty when nothing fits (the caller skips).
    """
    lines = content.splitlines()
    total = len(lines)
    marker_worst = len(f"… ({total} more lines)")
    budget_chars = token_cap * 4 - marker_worst
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget_chars:
            break
        kept.append(line)
        used += len(line) + 1
    if not kept:
        return ""
    return "\n".join(kept) + f"\n… ({total - len(kept)} more lines)"


def _hunk_ranges_by_file(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Target-side (start, length_inclusive_end) ranges per file from a diff.

    TARGET side on purpose: the excerpt is read from the worker's checkout,
    which at review time holds the PR head merged onto the base (the review
    scaffold checks the merged head out and detaches at it), so the applied
    hunks live at their target lines. SOURCE-side numbers would name lines
    of the pre-change file that the worktree no longer contains. A hunk with
    ``target_length == 0`` (pure deletion) has no lines in the head file and
    is skipped for the same reason. If the base tip has moved past the PR's
    merge base the merged file drifts from the PR head and both sides are
    approximations; the ±_EXCERPT_PAD_LINES window absorbs small drift.

    One malformed file does not sink the rest: the diff is parsed per
    ``diff --git`` section, and a section that fails to parse is logged at
    INFO and skipped (a single bad hunk used to raise for the whole diff,
    silently dropping every file's excerpt at DEBUG).
    """
    if not diff.strip():
        return {}
    ranges: dict[str, list[tuple[int, int]]] = {}
    for section in _diff_sections(diff):
        try:
            from unidiff import PatchSet  # type: ignore[import-untyped]

            patch = PatchSet(section)
        except Exception:
            logger.info(
                "context: one diff section failed to parse; its file gets no excerpt",
                exc_info=True,
            )
            continue
        for patched_file in patch:
            spans = [
                (hunk.target_start, hunk.target_start + hunk.target_length - 1)
                for hunk in patched_file
                if hunk.target_length > 0
            ]
            if spans:
                ranges[patched_file.path] = spans
    return ranges


def _diff_sections(diff: str) -> list[str]:
    """The diff split into per-file sections at ``diff --git`` line starts.

    Line starts only: a hunk body can quote the marker mid-line (a test
    fixture that contains a diff), and a raw string split would cut the
    section there. Each section keeps its own header so it parses alone.
    """
    if not diff.strip():
        return []
    sections: list[list[str]] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and sections:
            sections.append([line])
        elif line.startswith("diff --git "):
            sections = [[line]]
        elif sections:
            sections[-1].append(line)
        else:
            # Text before the first header: kept as one section so the
            # caller's parse fails and skips it, loudly.
            sections = [[line]]
    return ["".join(s) for s in sections if "".join(s).strip()]


def _excerpt_around(content: str, ranges: list[tuple[int, int]], token_cap: int) -> str:
    """The changed lines plus padding, joined with omission markers.

    Every part — markers, headers, and the join separators — is counted against
    the cap, so the caller's token check on the result cannot reject what this
    built. Windows are merged AFTER padding, so two nearby hunks emit their
    overlap once. When a window has to shrink, the padding goes first: the
    changed lines are the point of the excerpt, the padding is context around
    them.
    """
    lines = content.splitlines()
    n = len(lines)
    # Reserve the trailing marker's exact worst case (separator included) so
    # the final joined string stays within token_cap * 4 characters.
    budget_chars = token_cap * 4 - len(f"\n… lines {n}-{n} omitted")
    parts: list[str] = [f"(excerpt around the change; full file is {n} lines)"]
    used = len(parts[0])  # length of "\n".join(parts) so far

    def _add(part: str) -> None:
        nonlocal used
        parts.append(part)
        used += len(part) + 1  # +1 for the join separator

    # Merge the PADDED spans (the merge fires strictly more often than a raw
    # merge would, so sorting the raw spans is all the pre-work needed),
    # keeping each window's raw (change) bounds so the shrink path can drop
    # padding without touching the changed lines.
    windows: list[tuple[int, int, int, int]] = []  # (padded_start, padded_end, raw_start, raw_end)
    for raw_start, raw_end in sorted(ranges):
        if raw_start > n:
            # Hunk past EOF: the checkout is not at the diff's head commit.
            continue
        p_start = max(1, raw_start - _EXCERPT_PAD_LINES)
        p_end = min(n, raw_end + _EXCERPT_PAD_LINES)
        if windows and p_start <= windows[-1][1] + 1:
            w = windows[-1]
            windows[-1] = (w[0], max(w[1], p_end), min(w[2], raw_start), max(w[3], raw_end))
        else:
            windows.append((p_start, p_end, raw_start, raw_end))

    last_end = 0
    emitted = False
    for start, end, raw_start, raw_end in windows:
        gap = f"… lines {last_end + 1}-{start - 1} omitted" if start > last_end + 1 else ""
        header = f"lines {start}-{end}:"
        chunk = "\n".join(lines[start - 1 : end])
        cost = sum(len(p) + 1 for p in (gap, header, chunk) if p)
        if used + cost > budget_chars:
            # Padding first; if even the bare change does not fit, cut it at
            # a line boundary — a hunk larger than the whole per-file cap loses
            # something however it is cut. The header then says the lines that
            # were actually DELIVERED, never the window that was asked for:
            # the model grounds line citations in these headers, so a header
            # claiming lines that were silently cut is worse than the cut.
            for pad in (10, 4, 0):
                start = max(1, raw_start - pad)
                end = min(n, raw_end + pad)
                chunk = "\n".join(lines[start - 1 : end])
                header = f"lines {start}-{end}:"
                gap = f"… lines {last_end + 1}-{start - 1} omitted" if start > last_end + 1 else ""
                cost = sum(len(p) + 1 for p in (gap, header, chunk) if p)
                if used + cost <= budget_chars:
                    break
            else:
                # -3 for the chunk's own join separator and the trailing "…"
                # marker, so the joined result can never exceed the cap even
                # by a character.
                room = budget_chars - used - sum(len(p) + 1 for p in (gap, header) if p) - 3
                if room <= 0:
                    break
                chunk = chunk[:room].rsplit("\n", 1)[0] + "\n…"
                if not chunk.rstrip("\n…").strip():
                    # Only the marker survived the cut — nothing to show.
                    break
                # The header says the lines actually delivered, never the
                # window that was asked for: the model grounds line citations
                # in these headers.
                if "\n" in chunk.rstrip("\n…"):
                    end = start + chunk.rstrip("\n…").count("\n")
                    header = f"lines {start}-{end}:"
                else:
                    end = start
                    header = f"line {start}:"
        for part in (gap, header, chunk):
            if part:
                _add(part)
        last_end = end
        emitted = True
    if not emitted:
        # Every hunk was past EOF (checkout not at the diff's head) — there is
        # no windowable content here, not a marker-only "excerpt".
        return ""
    if last_end < n:
        _add(f"… lines {last_end + 1}-{n} omitted")
    return "\n".join(parts)


def _render(files: list[tuple[str, str]], heading: str) -> str:
    if not files:
        return ""
    blocks = [heading, ""]
    for rel, content in files:
        lang = _fence_lang(rel)
        blocks.append(f"### {rel}")
        blocks.append(f"```{lang}")
        blocks.append(content.rstrip("\n"))
        blocks.append("```")
        blocks.append("")
    return "\n".join(blocks).rstrip()


def _fence_lang(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".jsx": "jsx",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".scala": "scala",
        ".rb": "ruby",
        ".sh": "bash",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".toml": "toml",
        ".json": "json",
        ".md": "markdown",
    }.get(suffix, "")


def _safe_read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _autodetect_package_roots(repo_path: Path) -> list[str]:
    """Best-effort discovery of first-party package names.

    Looks under ``repo_path``, ``repo_path/src``, and ``repo_path/python``
    for directories containing ``__init__.py``. Returns sorted package names.
    Returns empty list if no Python packages are discoverable — in which case
    the resolver will produce no imports (graceful no-op).
    """
    roots: set[str] = set()
    for parent in (repo_path, repo_path / "src", repo_path / "python"):
        if not parent.is_dir():
            continue
        try:
            for child in parent.iterdir():
                if child.is_dir() and (child / "__init__.py").is_file():
                    roots.add(child.name)
        except OSError:
            continue
    return sorted(roots)


__all__ = ["build_context_strings", "estimate_tokens"]
