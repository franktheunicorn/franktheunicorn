# REVIEWER.md

Instructions for franktheunicorn when reviewing PRs to **its own repository**. This file defines the review norms, focus areas, and tone for the franktheunicorn project itself.

This is the project context that would go in `config/active/projects/franktheunicorn.yaml`. It's maintained as a markdown file in the repo so it's version-controlled and visible to contributors.

---

## Project Context

franktheunicorn is a Python/Django application. It's a developer tool, not a web app for end users. The primary audience is OSS maintainers who are technical and opinionated. The codebase should be clean, well-tested, and boring — no cleverness for cleverness's sake.

**Project type:** personal (maintained by a single operator with occasional contributors)

**Languages:** Python (backend, worker, CLI), HTML/CSS (Django templates), minimal vanilla JS (keyboard shortcuts)

---

## Review Focus Areas

### Always flag these — they are bugs or design violations

- **Missing dual-path implementation.** Any new data source that only has API access or only has scrape access is incomplete. Both paths are required with contract tests.
- **SQLite incompatibility.** Any Django ORM usage that breaks on SQLite (Postgres-specific field types, unsupported lookups). Test against SQLite, not Postgres.
- **Ungated feature access.** Any code that assumes a feature is configured (CodeRabbit, SMTP, Docker, JIRA) without checking first. All features must degrade gracefully.
- **Direct GitHub posting without review queue.** In v1, every comment goes through the dashboard. No code path should post to GitHub without operator approval.
- **Security: container escape vectors.** Any test execution code must use rootless Docker with `--network=none` and dropped capabilities. No Docker socket in the web container.
- **Security: secrets in logs or responses.** API keys, tokens, and credentials must never appear in log output, error messages, or dashboard responses.
- **Anti-pattern list bypass.** Any finding generation path that doesn't check the anti-pattern list before surfacing findings to the operator.

### Check carefully — these are common sources of subtle bugs

- **Rate limiter integration.** External API calls should go through the appropriate rate limiter. Check that both the static limiter and adaptive header reading are wired up.
- **Cache invalidation.** If something is cached (JIRA tickets, community context, collaborator scores), verify the invalidation logic or manual refresh mechanism exists.
- **Finding → GitHub comment mapping.** When findings span multiple lines, verify the `start_line` + `line` API parameters are set correctly. Single-line findings should not set `start_line`.
- **Worker resumability.** The worker must be safe to kill and restart at any point. Check for uncommitted database transactions, partial state, or resources that aren't cleaned up on unexpected exit.
- **Tone Guard ordering.** Tone Guard rewrites should happen *after* finding generation but *before* anti-pattern matching and queuing. The reasoning_trace should preserve the pre-rewrite text.
- **Config schema validation.** New config fields need schema validation in `config/schema.py` with clear error messages on invalid values.

### Good to catch but don't block the PR

- **Test coverage gaps.** Point them out but don't reject a PR solely for this if the code is otherwise correct and the contributor commits to a follow-up.
- **Docstring quality.** Prefer actionable suggestions ("consider adding a docstring explaining the return type") over generic complaints.
- **Import ordering.** `ruff` handles this — don't manually comment on import order.
- **Type hint completeness.** Flag missing type hints on public functions. Don't flag them on test helpers or internal single-use lambdas.

---

## Tone

This project reviews itself. The irony is not lost on us. Keep the tone:

- **Direct and technical.** This is a developer tool. The audience reads code for a living.
- **Constructive.** Always suggest how to fix, not just what's wrong. Include file paths and line references.
- **Aware of the meta-layer.** When reviewing changes to the review pipeline itself, note if a change would affect how the agent reviews other projects. A bug in Tone Guard affects every project the operator monitors.
- **Pragmatic.** A working v1 with rough edges beats a perfect design that ships in v2. Don't block PRs for style when the functionality is correct.
- **Respectful of AI-generated code.** Many PRs to this repo will be AI-generated (it's a tool for reviewing AI-generated PRs — of course the agents will contribute to it). Apply the same standards as human PRs: does it work, is it tested, does it follow the patterns? Don't penalize for "AI style" if the code is correct.

---

## Custom Scoring (for this repo specifically)

When scoring PRs to franktheunicorn itself, weight these higher:

- **Changes to `data_access/`:** These are the most fragile part of the system. GitHub HTML structure changes, API deprecations, and rate limit edge cases all live here.
- **Changes to `scoring/`:** Interest scoring directly affects what the operator sees. A bug here means important PRs get missed or noise floods the inbox.
- **Changes to `review/anti_patterns.py`:** The anti-pattern system is the core learning mechanism. Changes here affect every project the agent monitors.
- **Changes to `core/models.py`:** Schema changes have migration implications. Check for missing migrations and backward compatibility.
- **Changes to `worker/`:** The worker is the long-running process. Memory leaks, unclosed connections, and unhandled exceptions are critical here.
- **Changes to `dashboard/templates/`:** Must be tested on mobile. A broken template means the operator can't triage at conferences.

---

## What This Repo's Anti-Patterns Will Look Like

As the project matures, expect anti-patterns like:

```yaml
anti_patterns:
  - category: style-nit
    pattern: "Do not comment on Django template formatting — djlint handles it"
  - category: naming
    pattern: "Do not rename DataFetcher methods — the dual-path interface is stable"
  - category: test-coverage
    pattern: "Do not request tests for config_examples/ changes"
  - category: architectural
    pattern: "Do not suggest replacing htmx with a JS framework"
  - category: style-nit
    pattern: "Do not flag line length in test files with long fixture strings"
```

These will emerge from actual review feedback. Don't pre-populate — let them grow organically.

---

## Moderation Queue Notes

- **AI-generated PRs** are expected and welcome. Apply standard review criteria.
- **New contributors** should get a warm welcome + pointer to CLAUDE.md and AGENTS.md.
- **Low-context drive-bys** (PRs with no description, no linked issue) should get a polite request for context, not an immediate close suggestion. This is a young project — enthusiasm is welcome.
- **"Consider closing" queue** should only contain PRs that are genuinely stale (30+ days, no response to feedback). Don't rush.

---

## Differential Test Expectations

For this repo specifically:

- **All PRs touching `src/` must include or modify tests.** No exceptions.
- **Data access PRs must include both API and scrape path tests** with fixture files.
- **Dashboard PRs must include Django test client tests** verifying the response status and key content elements.
- **Worker PRs should include a test demonstrating resumability** — start a job, kill the worker, restart, verify state is consistent.

The differential test runner should flag any PR where new tests pass on both the PR branch and the base branch. This is especially important for this repo since many changes are to internal logic that's easy to write tests for but hard to write *meaningful* tests for.

---

## Meta: Eating Our Own Dog Food

This repository is both the product and the first customer. When franktheunicorn is stable enough to review its own PRs, enable it. The project YAML should use this REVIEWER.md as the `review_context` source. Any pain points discovered while self-reviewing should be filed as issues and prioritized — if the tool can't review its own repo well, it can't review anyone else's.
