# Codebase Audit: franktheunicorn vs. Spec

Audit date: 2026-04-01
Auditor: Claude Code
Compared: all source files, tests, config, and infrastructure against `docs/franktheunicorn-master-design.md` and `CLAUDE.md`.

---

## 1. MISSING FEATURES (spec'd but not implemented)

### 1.1 v1 Features Missing

| # | Issue | Spec Reference | Details |
|---|-------|---------------|---------|
| 1 | **No `config_examples/` directory** | CLAUDE.md project structure | CLAUDE.md says `config_examples/` should have example YAML configs for solo-maintainer + team-lead personas. Directory doesn't exist. The `config/examples/` path is referenced in `settings.py` but CLAUDE.md lists `config_examples/` at the project root. |
| 2 | **GitHub data_access missing integration tests** | CLAUDE.md testing conventions | Every DataFetcher should have three test files: `test_*_api.py`, `test_*_scrape.py`, `test_*_integration.py`. GitHub has no `test_*_integration.py` files testing the unified `fetch()` with fallback behavior. |
| 3 | **GitHub blame fetcher missing scrape path** | Design doc 2.5, 7.3 | `scoring/blame_fetcher.py` exists but the spec requires dual-path (API + scrape) for blame data in `data_access/github/`. There's no `data_access/github/blame_fetcher.py` with both paths and contract tests. |
| 4 | **GitHub user info fetcher missing entirely** | Design doc 7.3 | The source matrix lists "GitHub user info" with API + scrape paths and indefinite caching. No user info fetcher exists anywhere. |
| 5 | **No `record-fixtures` management command** | CLAUDE.md, Design doc 7.4 | `review-agent record-fixtures` should generate test fixtures from live data and redact tokens. Not implemented. |
| 6 | **No `worker status` command** | Design doc 19 CLI Reference | `run_worker` exists but no `worker status` showing queue depth and rate limit status. |
| 7 | **No `regenerate-scoring` command** | Design doc 2.9, 19 | Custom scoring function generation from natural language descriptions is spec'd for v1 but not implemented. Only the sandbox evaluator for pre-written expressions exists. |
| 8 | **Dockerfile not verified** | `compose.yaml` references `docker/Dockerfile` | The compose file references `target: web` and `target: worker` multi-stage targets. Need to verify the Dockerfile actually implements these stages correctly. |
| 9 | **No comment recall feature** | Design doc 9.4 | "Recall" button to delete posted comments via GitHub API within 24h window, using `<!-- franktheunicorn-managed -->` markers. No implementation in views or templates. |
| 10 | **Keyboard shortcuts may be incomplete** | Design doc 9.3 | `shortcuts.js` exists but the spec requires: `j/k` navigate findings, `a` approve, `e` edit, `r` reject, `n/p` next/prev PR, `s` post review, `x` expand/collapse, `A` approve-all-nits, `?` help overlay. Needs verification of completeness. |
| 11 | **No ET Phone Home / telemetry** | Design doc 13 | Optional telemetry system is spec'd (enabled by default, endpoint doesn't exist yet, clearly disclosed during init). No implementation. |
| 12 | **No workspace-aware digest** | Design doc 2.8, 12 | Digest service exists but doesn't support per-workspace schedules or workspace filtering. Design doc shows separate digests for different workspaces at different times. |
| 13 | **`near_operator_code` blame Layer 2 missing** | Design doc 2.5 | Only Layer 1 (direct authorship via `touches_operator_code`) is implemented. Layer 2 (proximity-based scoring for changes within N lines of operator's code) is spec'd for v1 but missing from `scoring/blame.py`. |
| 14 | **No cost stats in digest** | Design doc 12 | Weekly digest should include a cost tracking section (`~$4.20 LLM, 45 min containers`). `digest/service.py` doesn't query `CostRecord` at all. |
| 15 | **No stale anti-pattern alerts in digest** | Design doc 12 | Monthly section should flag anti-patterns that haven't matched in 60 days. Not implemented in digest service. |
| 16 | **No `check-rate-limits` command** | Design doc 19 CLI Reference | `review-agent check-rate-limits` should show current rate limit status across all services. Not implemented. |
| 17 | **No `test-scoring` command** | Design doc 19 CLI Reference | `review-agent test-scoring --project spark --pr 54102` for testing custom scoring against a specific PR. Not implemented. |
| 18 | **Settings doesn't support DATABASE_URL for Postgres** | CLAUDE.md, Design doc | CLAUDE.md says "Postgres optional via `DATABASE_URL`." Settings hardcodes SQLite with no `DATABASE_URL` fallback. |

### 1.2 v1.25 Features (partially implemented)

| # | Issue | Details |
|---|-------|---------|
| 19 | **Session feedback is record-only** | `send_feedback` view in `dashboard/views.py` records `AgentFeedback` in the DB but never actually opens the session URL in a browser or calls any external API. The entire point of v1.25 is direct delivery to the agent session. |
| 20 | **No Codex API integration** | Design doc 3.5 mentions Codex task ID feedback via API call. Only `session-url` and `github-comment` feedback methods exist. No actual API call to Codex. |

### 1.3 v1.5 Features (partially implemented)

| # | Issue | Details |
|---|-------|---------|
| 21 | **Context orchestrator not wired into worker** | `data_access/context_orchestrator.py` exists but the worker's `_run_cycle` never calls it. `build_pr_context` in `drafter.py` accepts `community_context`/`jira_context`/`sentry_context` strings but they're always empty because the worker doesn't fetch them. |
| 22 | **Auto-poster triple gate may be incomplete** | `review/auto_poster.py` exists. Design doc requires triple gate: confidence threshold + anti-pattern clean + tone guard passed. Need to verify all three gates are checked. |
| 23 | **Discord fetcher has no scrape path (by design)** | `DiscordFetcher.fetch_via_scrape` raises `NotImplementedError`. Design doc 7.3 says Discord is "API-only; no public web scraping" — this is correct per spec. Document this clearly in the fetcher docstring. |

### 1.4 Version Boundary Concerns

| # | Issue | Details |
|---|-------|---------|
| 24 | **v2 models in production migrations** | `0007_v2_shepherding_merge_queue.py` adds shepherding and merge queue fields. These are v2 per the roadmap. The code works but v2 features should ideally be behind feature gates or in separate branches. |
| 25 | **Fine-tuning fully implemented (v2 scope)** | The entire `fine_tuning/` module plus management commands `fine_tune` and `export_training_data` are implemented. Spec says v2. |
| 26 | **Merge queue fully implemented (v2 scope)** | `worker/merge_queue.py`, dashboard views `merge_queue_view` and `merge_pr` — all v2 scope. |
| 27 | **Shepherding fully implemented (v2 scope)** | `review/shepherding.py`, `_run_shepherding_pass` in worker — v2 scope. |
| 28 | **Curator module implemented (v1.5 scope)** | Full Textual TUI for voice bootstrapping in `curator/`. Spec says v1.5. |

---

## 2. CODE QUALITY ISSUES

### 2.1 Data Model Mismatches vs. Design Doc

| # | Issue | File | Details |
|---|-------|------|---------|
| 29 | **`sources` field missing on ReviewDraft** | `core/models.py:167` | Design doc 3.2 says findings have `sources: ["agent-primary", "agent-fast", "coderabbit", ...]` as a JSONField list. ReviewDraft has `source` (singular CharField). This breaks multi-source dedup tracking per 3.3. |
| 30 | **Missing `project_type` field on Project** | `core/models.py:17` | Design doc 14 shows `project_type = CharField(max_length=20)` with values `asf`, `personal`, `org`. The model only has `owner`, `repo`, `review_context`, `enabled`. This field is used in the rejection predictor feature extraction and digest grouping. |
| 31 | **`has_test_coverage` missing from PullRequest** | `core/models.py:41` | Design doc 14 lists `has_test_coverage = BooleanField(null=True)`. Not on the model. Used for "no test coverage" badge. |
| 32 | **`config_yaml` missing from Project** | `core/models.py:17` | Design doc 14 lists `config_yaml = TextField()` for storing the project's YAML config. Not on the model. |
| 33 | **`name` missing from Project** | `core/models.py:17` | Design doc 14 shows `name = CharField(unique=True)` (e.g., "apache-spark"). Model only has `owner`/`repo`. Used in config references and the full UI. |

### 2.2 Scoring Signal Mismatches

| # | Issue | File | Details |
|---|-------|------|---------|
| 34 | **`ai_generated` signal weight is negative** | `scoring/signals.py:24` | Design doc 2.1 shows `ai_generated: 10` (positive — flag for extra review). Code has `-10` (penalty that pushes AI PRs down). The spec says AI PRs should get *extra* review attention and route to the AI queue, not be deprioritized. |
| 35 | **`touches_operator_code` weight mismatch** | `scoring/signals.py:19` | Design doc 2.5 updated table shows weight 20. Code has 15. |
| 36 | **`path_overlap` uses prefix match, not glob** | `scoring/signals.py:64` | `f.startswith(wp)` doesn't handle glob patterns like `python/pyspark/**`. The project config schema and design doc use glob patterns in `watch_paths`. Should use `fnmatch` or `pathlib.PurePath.match`. |

### 2.3 Other Code Issues

| # | Issue | File | Details |
|---|-------|------|---------|
| 37 | **No `TestRun` link to findings** | Design doc 8 | Design doc says test results are "attached to relevant findings when ready." No FK or mechanism connects `TestRun` to specific `ReviewDraft` rows. |
| 38 | **Naming: ReviewDraft vs ReviewFinding** | `core/models.py:130` | Design doc calls the core abstraction `ReviewFinding`. Code uses `ReviewDraft`. The docstring acknowledges this but it creates confusion throughout — the codebase references both names inconsistently. Either rename the model or add a type alias. |
| 39 | **Two conflicting migration branches resolved via merge** | `core/migrations/` | `0005_reviewdraft_code_context_and_more.py` and `0005_v15_context_fields.py` are parallel branches merged by `0006_merge_20260401_1720.py`. Works but is messy — consider squashing before release per CLAUDE.md guidance. |

---

## 3. TESTING GAPS

| # | Issue | Details |
|---|-------|---------|
| 40 | **No `test_drafter.py`** | The review drafter (`review/drafter.py`) is the core pipeline orchestrator — it runs backends, deduplicates, applies tone guard, gates anti-patterns, and scores with the rejection predictor. No dedicated test file. |
| 41 | **No `test_worker_runner.py`** | Worker runner is excluded from coverage but `_run_cycle` is the main pipeline entry point. At minimum the cycle function should be tested with mocks. |
| 42 | **Missing integration tests for GitHub data_access** | Per spec, each DataFetcher needs `test_*_integration.py` testing the unified `fetch()` with API failures triggering scrape fallback. Missing for all GitHub fetchers. |
| 43 | **Missing integration tests for mailing_list** | Has API, scrape, contract, and cache tests but no `test_*_integration.py` for the unified fetch path. |
| 44 | **No Perplexity contract/scrape tests** | `tests/data_access/perplexity/` only has `test_perplexity_api.py`. No contract tests. (Perplexity is API-only per spec, but a contract test against the types would still be valuable.) |
| 45 | **No Sentry contract/scrape tests** | Same — only `test_sentry_api.py`. No contract test against the response types. |
| 46 | **No tests for dependency service end-to-end** | `tests/data_access/dependencies/` has unit tests for individual pieces but no integration test of the full `detect_and_fetch_changelogs` flow. |
| 47 | **Factory completeness** | `tests/factories.py` should cover all models. Need to verify `DependencyChange`, `AgentFeedback`, `TestRun`, `CostRecord` all have factories. |

---

## 4. STRUCTURAL / CONVENTION ISSUES

| # | Issue | Details |
|---|-------|---------|
| 48 | **Test files in wrong location** | CLAUDE.md says tests should be in `tests/core/`, `tests/scoring/`, `tests/review/`, `tests/data_access/`, `tests/worker/`, `tests/dashboard/`. Most test files are flat at `tests/` root (e.g., `tests/test_signals.py` instead of `tests/scoring/test_signals.py`). This affects discoverability and contradicts the project structure in CLAUDE.md. |
| 49 | **`github/` module duplicates `data_access/github/`** | `src/franktheunicorn/github/` has `client.py`, `mock.py`, `poller.py`, `poster.py`. `src/franktheunicorn/data_access/github/` has `pr_fetcher.py`, `diff_fetcher.py`, `review_fetcher.py`, `issue_fetcher.py`. The spec shows a single `data_access/github/` module. The split creates confusion about where GitHub functionality lives. |
| 50 | **Empty `management/` package at top level** | `src/franktheunicorn/management/commands/__init__.py` exists but is empty. All commands are under `core/management/commands/`. The empty package is dead code. |
| 51 | **`downstream` scoring not in `signals.py`** | `score_downstream_impact` is imported from `scoring/downstream.py` in the scorer, but all other signal functions live in `signals.py`. Inconsistent pattern. |
| 52 | **No shared `data_access/conftest.py`** | Spec shows a shared `data_access/conftest.py` for fetcher factories. Only per-source conftest files exist. |
| 53 | **No CSS file for dashboard** | Templates reference styling but there's no dedicated CSS file in `dashboard/static/`. Styles appear to be inline in templates. CLAUDE.md says "CSS custom properties for theming." |

---

## 5. DOCUMENTATION / CONFIG GAPS

| # | Issue | Details |
|---|-------|---------|
| 54 | **No example configs for personas** | CLAUDE.md and design doc 15.1 both reference example configs for solo-maintainer and team-lead personas. None exist in the repo. |
| 55 | **`.env.example` missing SMTP vars** | Design doc 12.3 lists `REVIEW_AGENT_SMTP_HOST`, `REVIEW_AGENT_SMTP_PORT`, `REVIEW_AGENT_SMTP_USER`, `REVIEW_AGENT_SMTP_PASS`, `REVIEW_AGENT_EMAIL_FROM`. Not in `.env.example` (though settings.py reads them). |
| 56 | **`.env.example` missing `DATABASE_URL`** | Design doc mentions Postgres via `DATABASE_URL`. Not in `.env.example`. |
| 57 | **`setup.sh` needs verification** | `scripts/setup.sh` exists. Should verify it works end-to-end with the current codebase state. |

---

## 6. PRIORITY RANKING

### P0 — Core v1 bugs and scoring mismatches (fix first)
- **#34**: `ai_generated` weight is negative, contradicts spec (should be +10)
- **#35**: `touches_operator_code` weight is 15, spec says 20
- **#36**: `path_overlap` uses prefix match instead of glob matching
- **#29**: `sources` field should be JSONField list, not singular CharField
- **#18**: Settings should support `DATABASE_URL` for Postgres
- **#30**: Missing `project_type` on Project model (used by rejection predictor)

### P1 — Missing v1 features that affect core functionality
- **#1, #54**: Config examples needed for onboarding
- **#2, #42, #43**: Missing integration tests for DataFetchers
- **#3**: Blame fetcher needs dual-path in data_access layer
- **#4**: GitHub user info fetcher not implemented
- **#5, #16, #17**: Missing CLI management commands
- **#9**: Comment recall feature
- **#13**: Blame Layer 2 (proximity-based) scoring
- **#14, #15**: Digest missing cost stats and stale anti-pattern alerts
- **#21**: Context orchestrator not wired into worker pipeline
- **#40, #41**: Critical test coverage gaps (drafter, worker)

### P2 — Code quality and structural cleanup
- **#31, #32, #33**: Missing model fields vs design doc
- **#37, #38, #39**: Model relationship gaps and naming confusion
- **#44, #45, #46, #47**: Test coverage gaps for newer features
- **#48**: Reorganize flat test files into proper subdirectories
- **#49, #50, #51, #52, #53**: Structural cleanup
- **#55, #56, #57**: Documentation gaps

### P3 — Version boundary decisions (policy, not bugs)
- **#24-28**: v1.5/v2 features implemented ahead of schedule. Decide: keep (with feature gates disabled by default) or extract to feature branches. All these features have `enabled: false` defaults, so they're functionally gated already.

---

## 7. AGENT TASK SPECIFICATIONS

Each item above is scoped for a single agent session. For complex items, here's additional guidance:

**#34 (ai_generated weight)**: Change `"ai_generated": -10` to `"ai_generated": 10` in `scoring/signals.py:24`. Update `score_ai_generated` to return a positive value. Update any tests in `test_signals.py` that assert the negative weight.

**#36 (glob matching)**: Replace `f.startswith(wp)` with `fnmatch.fnmatch(f, wp)` in `path_overlap_fraction`. Import `fnmatch`. Update tests to cover glob patterns like `python/pyspark/**`.

**#29 (sources field)**: Add migration to change `source = CharField` to `sources = JSONField(default=list)` on ReviewDraft. Update all code that reads/writes `source` to use `sources` list. Update drafter, views, admin, factories, and all tests.

**#18 (DATABASE_URL)**: In `settings.py`, check for `DATABASE_URL` env var. If present, parse it with `dj-database-url` (add to dependencies). Fall back to SQLite if not set. Add `dj-database-url` to `pyproject.toml` dependencies.

**#48 (test reorganization)**: Move flat test files into subdirectories matching CLAUDE.md structure. Update any imports. Verify pytest discovery still works.
