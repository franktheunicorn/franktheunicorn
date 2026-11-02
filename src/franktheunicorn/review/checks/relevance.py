"""Deterministic relevance gates for LLM sub-checks.

Each sub-check asks an LLM about a specific subject — vulnerabilities, API
usage, test coverage, doc accuracy. When a PR's changed files cannot contain
that subject, the call is waste: a docs-only PR cannot introduce a buffer
overflow, and asking a model to find one both costs tokens and invents
findings out of nothing. This module is the cheap, deterministic answer to
"does this check's subject matter exist in this diff?" — decided from the
stored ``changed_files`` list, before any prompt is built.

Two rules keep this safe:

- **Unknown means run.** An empty ``changed_files`` (a degraded row, an old
  ingest) makes every check apply. The gate may only skip on positive
  evidence that nothing relevant changed, never on missing data.
- **PR-metadata checks are never gated.** ``pr-description`` and
  ``issue-link`` read the PR itself, and ``malicious-prompt`` scans title,
  body and diff text — prompt injection lives in prose as happily as in
  code, and its regex pre-filter is nearly free. They always apply.
"""

from __future__ import annotations

from collections.abc import Sequence

#: Source-code extensions. A PR touching only files outside this set cannot
#: change program behaviour, so the code-semantics checks have nothing to say.
_CODE_EXTS = frozenset(
    {
        ".py",
        ".pyi",
        ".java",
        ".scala",
        ".sc",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".cs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".kt",
        ".kts",
        ".swift",
        ".m",
        ".mm",
        ".sh",
        ".bash",
        ".sql",
        ".pl",
        ".pm",
        ".r",
        ".lua",
        ".dart",
        ".erl",
        ".hrl",
        ".ex",
        ".exs",
        ".hs",
        ".ml",
        ".mli",
        ".fs",
        ".fsx",
        ".vb",
        ".asm",
        ".s",
        ".gradle",
        ".groovy",
        ".vue",
        ".svelte",
    }
)

#: Configuration / build files. Security checks care about these because
#: dependency and workflow changes are a real attack surface (a poisoned
#: action, a bumped vulnerable package) even though no source line moved.
_CONFIG_EXTS = frozenset(
    {
        ".yml",
        ".yaml",
        ".json",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".config",
        ".xml",
        ".lock",
        ".properties",
        ".env",
        ".gemspec",
    }
)

_DOC_EXTS = frozenset({".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc"})

#: Well-known build/infra files whose extension alone would mislead
#: (requirements.txt is not a doc, Dockerfile has no extension at all).
_CONFIG_BASENAMES = frozenset(
    {
        "dockerfile",
        "containerfile",
        "makefile",
        "cmakelists.txt",
        "pom.xml",
        "build.gradle",
        "settings.gradle",
        "gradle.properties",
        "requirements.txt",
        "go.mod",
        "go.sum",
        "cargo.toml",
        "cargo.lock",
        "gemfile",
        "gemfile.lock",
        "pipfile",
        "pipfile.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "compose.yaml",
        "compose.yml",
        "docker-compose.yml",
        "docker-compose.yaml",
        "vagrantfile",
        "brewfile",
        "rakefile",
    }
)

#: CI workflow files: YAML by extension, but the prefix is the stronger signal
#: — script-injection in a workflow is a code-level security issue.
_WORKFLOW_MARKERS = (".github/workflows/", "/.github/workflows/")


def categorize_path(path: str) -> str:
    """Classify one changed path as ``code``, ``config``, ``docs`` or ``other``.

    Precedence is deliberate: an explicit config basename (``Dockerfile``,
    ``requirements.txt``) wins over its misleading extension, and the workflow
    prefix wins over everything — a PR that only edits
    ``.github/workflows/ci.yml`` still gets the full security pass.
    """
    normalized = path.strip().replace("\\", "/").lower()
    if not normalized:
        return "other"
    if any(marker in normalized for marker in _WORKFLOW_MARKERS):
        return "config"
    basename = normalized.rsplit("/", 1)[-1]
    if basename in _CONFIG_BASENAMES:
        return "config"
    dot = basename.rfind(".")
    ext = basename[dot:] if dot > 0 else ""
    if ext in _CODE_EXTS:
        return "code"
    if ext in _CONFIG_EXTS:
        return "config"
    if ext in _DOC_EXTS:
        return "docs"
    return "other"


#: Which file categories each check needs. A check runs when at least one
#: changed file matches one of its categories. Checks absent from this map
#: always run — see the module docstring for why pr-description, issue-link
#: and malicious-prompt are in that set.
CHECK_REQUIRED_CATEGORIES: dict[str, frozenset[str]] = {
    # Security family: vulnerabilities live in code and unsafe configuration;
    # markdown cannot contain either.
    "security": frozenset({"code", "config"}),
    "security-context": frozenset({"code", "config"}),
    "security-disclosure": frozenset({"code", "config"}),
    "cve-class": frozenset({"code", "config"}),
    "red-team": frozenset({"code", "config"}),
    # Code-semantics checks: nothing to say about a diff with no source.
    "api-misuse": frozenset({"code"}),
    "concurrency": frozenset({"code"}),
    "correctness": frozenset({"code"}),
    "edge-case": frozenset({"code"}),
    "design": frozenset({"code"}),
    "parity": frozenset({"code"}),
    "simplification": frozenset({"code"}),
    "error-framework": frozenset({"code"}),
    "coverage": frozenset({"code"}),
    "backport": frozenset({"code"}),
    # Version pins and config docs live in build files as much as in source.
    "test-pinning": frozenset({"code", "config"}),
    "config-docs": frozenset({"code", "config"}),
    # Doc accuracy is the check docs-only PRs keep: prose is its subject,
    # and doc comments ride along in code files.
    "doc-accuracy": frozenset({"code", "docs"}),
}


def check_applies(check_name: str, changed_files: Sequence[str]) -> bool:
    """Whether *check_name*'s subject matter can exist in *changed_files*.

    An empty file list means "unknown", not "nothing relevant" — every check
    applies rather than risk a silent skip on a data gap.
    """
    required = CHECK_REQUIRED_CATEGORIES.get(check_name)
    if required is None or not changed_files:
        return True
    return any(categorize_path(path) in required for path in changed_files)


def describe_required(check_name: str) -> str:
    """Human-readable category list for a check, for skip log lines."""
    required = CHECK_REQUIRED_CATEGORIES.get(check_name)
    return "/".join(sorted(required)) if required else "any"
