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
) -> tuple[str, str]:
    """Return ``(full_file_context, imported_modules_context)``.

    Both empty strings when the feature is disabled, the checkout is missing,
    or no eligible content fits the budget. ``config`` is duck-typed against
    ``ContextConfig`` to avoid importing config at module load.
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

    remaining = total_budget
    repo_path = Path(repo_path)

    # Stage 1: read changed files, smallest first, into the full-file pool.
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
                if tokens > per_file_cap or tokens > remaining:
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

    Looks under ``repo_path`` and ``repo_path/src`` for directories containing
    ``__init__.py``. Returns sorted package names. Returns empty list if no
    Python packages are discoverable — in which case the resolver will produce
    no imports (graceful no-op).
    """
    roots: set[str] = set()
    for parent in (repo_path, repo_path / "src"):
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
