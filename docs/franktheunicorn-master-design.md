# franktheunicorn v3 — Master Design Document

> **This is the consolidated design document.** It merges the base design (v3.0)
> with all four additions docs (v3.1 through v3.3) into a single reference.
> All open questions have been resolved. All decisions are locked.

---

## Table of Contents

- [Overview](#overview)
- [Prior Art & Lineage](#prior-art--lineage)
- [Product Evolution](#product-evolution)
- [Key Design Decisions](#key-design-decisions)
- [1. Project Context System](#1-project-context-system)
- [2. Interest Scoring & PR Routing](#2-interest-scoring--pr-routing)
- [2.4 Auto-Detect Collaborators](#24-auto-detect-collaborators)
- [2.5 Git Blame-Based Watching](#25-git-blame-based-watching)
- [2.6 New Contributor Handling](#26-new-contributor-handling)
- [2.7 "Committer Is On It" Down-Ranking](#27-committer-is-on-it-down-ranking)
- [2.8 Workspace Mode (Project Filtering)](#28-workspace-mode-project-filtering)
- [2.9 Custom Scoring Functions (LLM-Generated)](#29-custom-scoring-functions-llm-generated)
- [3. Review Findings (Core Abstraction)](#3-review-findings-core-abstraction)
- [3.5 Direct Agent Feedback Channel (v1.25)](#35-direct-agent-feedback-channel-v125)
- [4. Tone Guard](#4-tone-guard)
- [5. Learning from Corrections (Three-Tier System)](#5-learning-from-corrections-three-tier-system)
- [6. Voice Bootstrapping (Comment Curator)](#6-voice-bootstrapping-comment-curator)
- [7. Data Access Layer (Dual-Path: API + Scrape)](#7-data-access-layer-dual-path-api--scrape)
- [8. Multi-Backend Review Pipeline](#8-multi-backend-review-pipeline)
- [9. Differential Test Verification](#9-differential-test-verification)
- [9.5 Container Security Model](#95-container-security-model)
- [9.6 Per-Project Container Images + Auto-Build](#96-per-project-container-images--auto-build)
- [10. Fine-Tuning with Axolotl](#10-fine-tuning-with-axolotl)
- [10.8 Fine-Tuning: Dataset & Learning Patterns](#108-fine-tuning-dataset--learning-patterns)
- [11. Dashboard (Django)](#11-dashboard-django)
- [12. Daily Email Digest](#12-daily-email-digest)
- [12.5 Alert Mode](#125-alert-mode)
- [13. ET Phone Home (Optional Telemetry)](#13-et-phone-home-optional-telemetry)
- [14. Data Model (Django)](#14-data-model-django)
- [15. Self-Hosting & Onboarding](#15-self-hosting--onboarding)
- [16. Implementation Phases](#16-implementation-phases)
- [17. Testing Strategy](#17-testing-strategy)
- [17.5 Dual-Path Testing (API + Scrape)](#175-dual-path-testing-api--scrape)
- [18. State Directory](#18-state-directory)
- [19. CLI Reference (Complete)](#19-cli-reference-complete)
- [20. Resolved Design Decisions](#20-resolved-design-decisions)
- [21. Research Items](#21-research-items)

> **Security:** For the full threat model, trust boundary map, attack surfaces, remote CI trust
> rules, and hardening checklist, see **[Security Design](security-design.md)**.

---

## Overview

A local-first, cloneable AI code review assistant for open-source maintainers. Monitors PRs across multiple projects, triages by relevance, drafts review comments in the operator's voice, learns from feedback, and surfaces what needs attention via a lightweight dashboard and daily email digest.

**This is not a hosted service or a global bot.** It's a tool you clone, configure for your repos, and run on your own machine or server. State lives in SQLite and local files. There is no account to create, no SaaS dependency, no uptime requirement.

The reference operator is an Apache Spark committer/PMC member who also maintains smaller projects (Fight Health Insurance, spark-testing-base, personal infra repos). The agent must understand that ASF governance, a Django side project, and a personal Kubernetes repo are fundamentally different review contexts.

### Core Principles

1. **Local-first.** SQLite + local files are first-class, not a stepping stone to Postgres. The entire system state is a directory you can back up, rsync, or check into git.

2. **Clone and run.** `git clone`, set two env vars, `docker compose up`. Working dashboard in five minutes. Every feature beyond PR ingestion and LLM review is opt-in.

3. **Operator-in-the-loop.** The agent drafts; the human posts. Auto-posting exists but is off by default and gated behind multiple safety checks.

4. **Project-aware.** Each repo has its own review norms, tone, test expectations, and community. The agent adapts per-project, not globally.

5. **Learns from corrections.** The anti-pattern list is the primary feedback mechanism. Every rejected or edited comment makes the agent better. Fine-tuning is a later optimization, not a prerequisite.

---

## Prior Art & Lineage

This agent is the third generation of a line of code review tooling:

1. **[holdensmagicalunicorn](https://github.com/holdenk/holdensmagicalunicorn)** (~2012) — GitHub bot that auto-fixed code and spelling mistakes using static analysis (pfff, Coccinelle). Rule-based, focused on auto-correction. OCaml + Perl + Python.

2. **[franktheunicorn/predict-pr-comments](https://github.com/franktheunicorn/predict-pr-comments)** (~2018–2019) — Predicts *where* in a PR review comments are likely to land, using code vectors and historical comment/mailing list data. Scala + Go, Spark for data processing, Kubeflow for serving. Presented at [FOSDEM 2019](https://archive.fosdem.org/2019/schedule/event/ml_on_code_code_review_mailing_list/) by Holden Karau and Kris Nova.

3. **This agent** (2026) — Evolves from "predict where comments go" to "draft the actual comments" using LLMs. The core insight carries forward: review attention is predictable and can be guided by project-specific signals and community context.

4. **[spark-prs.appspot.com](https://spark-prs.appspot.com/)** (Databricks, ~2014–present) — A PR triage dashboard for Apache Spark. Classifies PRs by component based on files modified, links JIRA issues, and shows committer activity. Source: [databricks/spark-pr-dashboard](https://github.com/databricks/spark-pr-dashboard). Key insight: if another committer is already actively reviewing a PR outside your area, down-rank it — don't pile on.

**What carries forward:** Auto-fix via GitHub `suggestion` blocks (from v1). Community discussion sources as review context (from v2). Both adapted for the LLM era.

---

## Product Evolution

```
v1:     Assistant (triage + draft + learn from corrections Tier 1)
          + workspace mode
          + committer-is-on-it deranking
          
v1.25:  Direct agent feedback channel (Claude Code / Codex sessions)

v1.5:   Memory + social awareness
          + community context search (mailing lists, Discourse, Discord,
            Perplexity, GitHub Issues, Sentry)
          + JIRA integration
          + CodeRabbit CLI with fuzzy cross-source dedup
          + cross-project downstream detection
          + confidence-gated auto-posting (triple gate)
          + Comment Curator CLI (Textual TUI)

v3:     CLI triage mode (deferred from v1.5)

v1.75:  Bayesian rejection model (Tier 2 learning)
          + auto-suppress low-value findings
          + rejection probability visible in dashboard

v2:     Maintainer hub
          + fine-tuned personal model (Tier 3 learning, Qwen2.5-Coder-7B default)
          + full shepherding mode for operator's PRs
          + merge queue with auto-merge
          + K8s Helm chart
```

---

## Key Design Decisions

| Decision | Resolution | Rationale |
|----------|------------|-----------|
| Architecture | **Local-first.** SQLite + files. No uptime dependency. Resumable worker, not always-on service. | Cloneable tool, not infrastructure |
| Comment posting | **Draft-only** in v1. Configurable auto-post via triple gate in v1.5 | Protects reputation; earns trust before automating |
| Auto-fixes | **GitHub `suggestion` blocks**, draft mode | Low friction for PR authors, full operator control |
| Attribution | **Configurable footer** ("generated with assistance of franktheunicorn"), independently configurable per posting mode | Transparency; can be turned off |
| CodeRabbit | **Local CLI** invocation (v1.5) | Clean dedup; operator controls billing |
| Web framework | **Django** | Operator's expertise; admin for free; clean SQLite→Postgres path |
| Dashboard access | **Tailscale/WireGuard**, no app-level auth | Network-level auth for single operator |
| Mobile UI | **Responsive required** | Conference use over Tailscale |
| Feedback loop | **Anti-pattern list is the core learning system.** Elevated to primary, not a side feature. Visible and editable in dashboard from day one. | Transparent, immediate, no training lag |
| Voice bootstrapping | **Comment Curator CLI** — scrape → rank → flag tone → human include/exclude/edit → `voice_curated.jsonl` | Curated dataset beats raw scraping |
| Fine-tuning | **Deferred to v2.** Anti-patterns + operator edits first. Fine-tune only after signal quality is established. | Premature fine-tuning on noisy data produces noisy models |
| Budget | **No cap in v1** — cost stats in weekly digest | Operator self-regulates from data |
| Cross-project deps | Spark → spark-testing-base, Snowflake connector (v1.5) | Track specific upstream APIs |
| Worker model | **Periodic polling + resumable jobs.** Not an always-on service. Can run via cron. | Local-first; no uptime requirement |
| Merge queue | **Deferred to v2** | Reduces v1 surface area |
| K8s deployment | **Deferred to v2** | Docker Compose is sufficient for v1 |
| Agent test coverage | **pytest + coverage.py, 90%+** | Practice what we preach |

---

## 1. Project Context System

Each monitored project gets a context profile — the backbone of the entire system.

### 1.1 Project Profile Schema

```yaml
# ~/.review-agent/projects/spark.yaml
project:
  name: apache-spark
  repo: apache/spark
  type: asf  # asf | personal | org

  watch_paths:
    - "python/pyspark/**"
    - "sql/catalyst/src/main/scala/org/apache/spark/sql/catalyst/expressions/**"
    - "connector/connect/**"
  watch_labels: ["CORE", "SQL", "PYTHON", "CONNECT"]

  collaborators:
    - huaxin-gao
    - dongjoon-hyun

  review_context: |
    Apache Spark uses a consensus-based review process. All API changes
    require a SPIP or JIRA discussion first. Scala API changes must have
    corresponding PySpark/SparkR/Connect implementations. The project is
    in an extended LTS period for 3.5.x — backport compatibility matters.
    Style: direct, technically precise. Always reference the JIRA ticket.
    Do NOT comment on formatting — scalafmt and ruff handle that.

  references:
    - name: Contributing Guide
      url: https://spark.apache.org/contributing.html
    - name: Versioning Policy
      url: https://spark.apache.org/versioning-policy.html

  # --- Tone Guard ---
  tone:
    objective: |
      Preserve directness and technical precision. Remove unnecessary
      abrasiveness, pedantic phrasing, and snark. This is volunteer work
      — respect the effort. Rewrite for constructiveness, don't suppress.
    # Dual-stage: (1) filter bad tone from training data during curation,
    # (2) rewrite drafts for constructiveness before queuing

  # --- Test execution ---
  tests:
    enabled: true
    container_image: ghcr.io/apache/spark-test:latest
    timeout_minutes: 45
    resource_tier: heavy
    skip_tags: ["SlowTest", "DocTests"]

  # --- Posting ---
  posting:
    mode: draft-only  # draft-only | confidence-gated (v1.5)
    suggested_changes:
      action: suggestion-block  # suggestion-block | direct-commit
    attribution:
      draft_approved: true
      auto_posted: true
      text: "Generated with assistance of franktheunicorn 🦄"

  # --- Anti-patterns (core learning system) ---
  anti_patterns_file: ~/.review-agent/projects/spark.anti-patterns.yaml

  # --- v1.5+ features (inactive in v1) ---
  # jira:
  #   enabled: true
  #   server: https://issues.apache.org/jira
  #   project_prefix: SPARK
  # community_sources:
  #   - type: mailing-list
  #     name: Spark dev@
  #     archive_url: https://lists.apache.org/list.html?dev@spark.apache.org
  #     timeout_seconds: 30
  # downstream:
  #   - project: spark-testing-base
  #     repo: holdenk/spark-testing-base
  #     tracked_apis_file: ~/.review-agent/cache/spark-testing-base-imports.json
  # coderabbit:
  #   enabled: true
  #   deduplicate: true
```

```yaml
# ~/.review-agent/projects/fhi.yaml
project:
  name: fight-health-insurance
  repo: totallylegitco/fhi
  type: personal
  watch_paths: ["**"]

  review_context: |
    Django project handling health insurance appeal letters. HIPAA-adjacent
    — flag any PR that touches user data handling, auth, or external API
    calls. Check for missing Django migrations when models change. Security
    is paramount: no secrets in code, no broad CORS, no raw SQL.
    Tone: casual but thorough. Users depend on this.

  tone:
    objective: |
      Casual and direct. Technical accuracy matters, corporate-speak doesn't.
      For external contributors: warm but honest.

  tests:
    enabled: true
    container_image: python:3.12-slim
    setup_commands:
      - pip install -r requirements.txt -r requirements-test.txt
      - python manage.py migrate --run-syncdb
    timeout_minutes: 15
    resource_tier: standard

  security:
    dependency_audit: true
    flag_patterns: ["CORS", "raw(", "SECRET", ".env"]

  posting:
    mode: draft-only
    suggested_changes:
      action: direct-commit
    attribution:
      draft_approved: false
      auto_posted: true
      text: "Generated with assistance of franktheunicorn 🦄"

  anti_patterns_file: ~/.review-agent/projects/fhi.anti-patterns.yaml
```

### 1.2 Operator Profile (Global)

```yaml
# ~/.review-agent/config.yaml
operator:
  github_username: holdenk
  email: holden@example.com
  timezone: America/Los_Angeles

  # --- LLM backends ---
  backends:
    primary:
      provider: anthropic
      model: claude-sonnet-4-20250514
      use_for: [review-comments, pr-summaries, interest-scoring,
                test-reference-extraction, tone-guard]
    reasoning:
      provider: anthropic
      model: claude-opus-4-20250514
      use_for: [architectural-review, correctness-arguments]
    fast:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      use_for: [style-nits, commit-message-check, label-suggestion]

  # --- Posting identities ---
  posting:
    draft_account:
      github_token_env: GITHUB_TOKEN
      identity: holdenk
    # auto_account (v1.5):
    #   github_token_env: GITHUB_TOKEN_BOT
    #   identity: holden-review-bot

  # --- Worker ---
  worker:
    concurrency: 3
    priority: interest-score
    poll_interval_active: 600    # 10 min
    poll_interval_quiet: 3600    # 1 hour
    # Worker is resumable: safe to kill and restart.
    # Can also be run via cron instead of as a daemon.

  # --- Notifications ---
  notifications:
    dashboard: true
    email_digest:
      enabled: true
      send_at: "07:00"
      include_sections:
        - high-interest-prs
        - your-prs-needing-action
        - moderation-queue
        - test-failures
        - ai-agent-prs
        - cost-stats         # weekly
        - stale-anti-patterns  # monthly

  # --- AI-generated PR tracking ---
  ai_agents:
    track_authors:
      - "copilot[bot]"
      - "claude-code[bot]"
      - "cortex-bot"
      - "dependabot[bot]"
    track_commit_trailers:
      - "Generated-by: Claude Code"
      - "Co-authored-by: Cortex"
```

---

## 2. Interest Scoring & PR Routing

Interest scoring determines what surfaces in the dashboard and in what order the worker processes PRs. But not everything is a score — some signals route PRs into distinct queues.

### 2.1 Scoring Signals

| Signal | Weight | Description |
|--------|--------|-------------|
| `is_operator_pr` | **P0** | Operator's own PR has new activity. Always surfaces. Always first. |
| `path_overlap` | 30 | PR touches files in `watch_paths` |
| `mentioned_or_assigned` | 25 | Operator @-mentioned or assigned as reviewer |
| `has_review_request` | 20 | Explicit review request from GitHub |
| `prior_review_history` | 15 | Operator has previously reviewed PRs from this author |
| `collaborator` | 15 | PR author is a frequent contributor or in `collaborators` list |
| `touches_operator_code` | 15 | PR modifies lines the operator authored (git blame) |
| `new_human_contributor` | 10 | Author's first or second PR to this project (positive bump — encourage newcomers) |
| `keyword_match` | 10 | PR title/body matches configured keywords |
| `ai_generated` | 10 | PR author or trailers match `ai_agents` config |

The `is_operator_pr` signal is not a score boost — it's a separate priority lane (see §2.3 Shepherding).

**Deliberately excluded:** Generic social scoring, LinkedIn integration, PR author "quality" scoring. These are either too brittle, irrelevant to OSS, or risk reinforcing contributor bias.

### 2.2 Moderation Flags & Routing Queues

Some signals don't affect ranking — they route PRs into different queues with different handling.

| Flag | Trigger | Queue | Handling |
|------|---------|-------|----------|
| `likely_ai_generated` | Author matches `ai_agents` or commit trailers match | AI-generated queue | Extra review pass for hallucinated imports, phantom files. Mandatory differential tests. "Send Feedback to Agent" button. |
| `likely_unowned` | PR open > N days, no reviewer assigned, author unresponsive | Consider-closing queue | Draft polite comment suggesting missing context, expectations, possible closure. |
| `low_context_driveby` | Small PR, no description, no linked issue, unfamiliar author | Needs-triage queue | Dashboard flags for quick assessment. Draft comment requesting context. |
| `new_contributor` | Author's first PR to project | New contributor queue | Tone Guard enforced strictly. Draft comment includes welcome language + project norms. |

These queues appear as tabs in the dashboard, not just score modifiers. Each queue can have its own default action templates.

### 2.3 Shepherding (Operator's Own PRs)

The operator's own PRs are a fundamentally different workflow — not reviewing someone else's code but tracking and responding to feedback on your own. In v1, this is a separate dashboard section ("Your PRs Needing Action") with:

- New comments from reviewers, highlighted by recency
- CI status with flaky-test detection
- Merge conflict detection
- Staleness alerts

In v2, this expands into a full shepherding mode: draft responses to reviewer questions, auto-rebase when base branch diverges, suggested follow-up actions.

---

## 2.4 Auto-Detect Collaborators

### Problem

Manually maintaining a `collaborators` list is tedious and goes stale. The agent should detect collaboration patterns from git history and review data.

### Signal Sources

| Signal | Weight | Source |
|--------|--------|--------|
| `mutual_reviews` | 40 | GitHub API: PRs where operator reviewed author's PRs AND author reviewed operator's PRs |
| `co_file_committers` | 25 | Git log: authors who frequently commit to the same files as operator (within last 6 months) |
| `co_authors` | 20 | Git log: `Co-authored-by` trailers mentioning both operator and the person |
| `review_frequency` | 10 | GitHub API: how often operator has reviewed this person's PRs (one-directional) |
| `mailing_list_interaction` | 5 | (v1.5) Community sources: threads where both operator and person participate |

### Output

```yaml
# ~/.review-agent/projects/spark.collaborators.yaml
# Auto-generated by: review-agent detect-collaborators --project spark
# Last updated: 2026-03-27
# Manual edits are preserved — auto-detect merges, doesn't overwrite

collaborators:
  - username: huaxin-gao
    score: 92
    signals: [mutual_reviews: 38, co_file: 24, co_author: 18, review_freq: 8, mailing_list: 4]
    auto_detected: true
  - username: dongjoon-hyun
    score: 85
    signals: [mutual_reviews: 35, co_file: 22, review_freq: 10]
    auto_detected: true
  - username: some-new-person
    score: 45
    signals: [co_file: 20, review_freq: 15]
    auto_detected: true
  - username: manually-added-person
    score: null
    auto_detected: false  # manually added, never overwritten by auto-detect
```

### CLI

```bash
review-agent detect-collaborators --project spark
  # Analyzes last 6 months of git history + GitHub review data
  # Merges with existing collaborators file (preserves manual entries)
  # Respects rate limits on GitHub API

review-agent detect-collaborators --project spark --dry-run
  # Shows what would change without writing

review-agent detect-collaborators --all
  # Runs for all configured projects
```

### Scheduling

- Manual trigger: `review-agent detect-collaborators --project <name>`
- Optional auto-schedule: weekly cron, configured in operator profile
- Auto-detect runs during `review-agent init` for initial setup

### Integration with Interest Scoring

The collaborator boost in §2.1 reads from this file. Instead of a binary "is/isn't collaborator," the score is weighted by the collaborator's score:

```python
collaborator_boost = (collaborator.score / 100) * COLLABORATOR_WEIGHT
# huaxin-gao (score 92) gets almost full 15-point boost
# some-new-person (score 45) gets ~7 points
# manually-added (score null) gets full boost (manual = trust)
```

---

## 2.5 Git Blame-Based Watching

### Problem

Static `watch_paths` are a blunt instrument. The operator's code is scattered across the repo. A PR modifying `core/src/main/scala/org/apache/spark/rdd/RDD.scala` might be highly relevant if the operator wrote the lines being changed, even if `core/` isn't in `watch_paths`.

### Three Layers of Blame-Based Interest

**Layer 1: Direct authorship (`touches_operator_code`)** — Already in §2.1 but now fleshed out.

The agent runs `git blame` on every file changed in the PR and checks if the operator authored any of the modified lines. This is the highest-signal blame-based signal.

Implementation: For each changed file in the PR diff, fetch blame data (API or scrape), check if any lines in the diff hunk's range are attributed to the operator. Cache blame data per-file with commit hash as cache key — invalidate when the file changes.

**Layer 2: Proximity (`near_operator_code`)** — New signal.

Even if the operator didn't write the exact modified lines, changes *near* their code are often relevant. "Near" = within N lines (configurable, default 20) of a line the operator authored.

This catches: refactors that move code around the operator's implementations, bug fixes in functions the operator wrote where the fix is in nearby helper code, new code inserted between the operator's functions.

```python
# Pseudocode for proximity scoring
for hunk in pr_diff.hunks:
    blame = get_blame(hunk.file, base_branch)
    for line in blame.lines_in_range(hunk.start - PROXIMITY, hunk.end + PROXIMITY):
        if line.author == operator:
            return NEAR_OPERATOR_CODE_BOOST  # e.g., 10 points
```

**Layer 3: Structural similarity (`similar_to_operator_code`)** — Deferred to v1.5.

Uses tree-sitter to parse ASTs and detect when a PR modifies code that is structurally similar to code the operator has written elsewhere in the repo. Examples: someone modifies a UDF implementation pattern that the operator created in a different file, or refactors a test structure the operator established.

This is expensive (requires AST parsing + similarity scoring) and complex to calibrate. Defer until blame layers 1 and 2 are validated.

### Configuration

```yaml
# In project config
blame_watching:
  enabled: true
  direct_authorship: true     # Layer 1 (always on when blame_watching enabled)
  proximity:
    enabled: true              # Layer 2
    context_lines: 20          # lines above/below to check
  structural_similarity:
    enabled: false             # Layer 3 (v1.5)
  
  # Performance: blame is expensive on large repos
  cache_blame: true            # cache blame data per file+commit
  max_files_per_pr: 50         # skip blame analysis for huge PRs
  exclude_paths:               # never blame-analyze these
    - "docs/**"
    - "*.md"
    - "*.txt"
```

### Updated Interest Scoring Table

| Signal | Weight | Description |
|--------|--------|-------------|
| `is_operator_pr` | **P0** | Operator's own PR. Always surfaces, always first. |
| `path_overlap` | 30 | PR touches files in `watch_paths` |
| `mentioned_or_assigned` | 25 | Operator @-mentioned or assigned |
| `has_review_request` | 20 | Explicit review request |
| `touches_operator_code` | 20 | **Blame Layer 1:** PR modifies lines authored by operator |
| `prior_review_history` | 15 | Operator has reviewed PRs from this author before |
| `collaborator` | 15 | Weighted by collaborator score from auto-detected file |
| `near_operator_code` | 10 | **Blame Layer 2:** PR modifies lines near operator's code |
| `new_human_contributor` | 10 | First/second PR from this author (positive bump) |
| `keyword_match` | 10 | PR title/body matches keywords |
| `ai_generated` | 10 | AI agent author or commit trailers |
| `downstream_impact` | 20 | (v1.5) Touches APIs tracked in downstream project |

### Performance Considerations

Git blame is O(file_size × history_depth) per file. For large repos like Spark:

- **Cache aggressively.** Blame data changes only when the file changes. Cache key: `(file_path, base_branch_head_commit)`. Store in SQLite.
- **Cap file count.** Skip blame for PRs touching >50 files (these are usually bulk refactors or generated code).
- **Exclude non-code.** Docs, configs, and generated files don't need blame analysis.
- **Background processing.** Blame analysis runs as part of the worker pipeline, not blocking dashboard rendering.

### Data Access: API + Scrape

Blame data is fetched via the dual-path system (§7):

- **API path:** `GET /repos/{owner}/{repo}/git/blame/{ref}/{path}` — GitHub's blame API (if available; note: GraphQL has better blame support via `blame` field on `Repository.object`).
- **Scrape path:** Parse the blame view at `https://github.com/{owner}/{repo}/blame/{ref}/{path}` — the HTML blame page is well-structured and scrapeable.

Both paths return the same `BlameResult` dataclass. Both are tested.

---

## 2.6 New Contributor Handling

To prevent over-optimizing for familiar authors and reinforcing existing contributor bias, the agent has explicit new-contributor awareness:

**Detection:** First or second PR from an author to a project (checked against local PR history in SQLite).

**Scoring:** `new_human_contributor` signal gives a positive 10-point bump (not a penalty). The goal is to ensure new contributors' PRs get reviewed, not deprioritized.

**Tone Guard enforcement:** When reviewing a new contributor's PR, the `new_contributor_addendum` from the project's tone config is appended. This shifts the review style toward: welcoming language, focus on the most important 2-3 issues (don't pile on nits), links to project conventions and contributing guides, and constructive suggestions rather than "this is wrong."

**"Consider Closing" protection:** New contributor PRs are never auto-routed to the consider-closing queue, even if they're low-context. They go to the new-contributor queue instead, ensuring a human makes the call.

---

## 2.7 "Committer Is On It" Down-Ranking

### Problem

Large OSS projects like Spark have multiple committers. If someone else is already actively reviewing a PR — especially one outside the operator's area of expertise — there's no need to surface it. The operator's attention is finite; spend it where it matters.

### Signal Detection

A PR has an active committer if:

- A known project committer (not the operator) has posted a review comment in the last 48 hours, AND
- The PR is not in the operator's `watch_paths` or `watch_labels`, AND
- The operator was not explicitly @-mentioned or assigned

If all three conditions are true, apply a **negative score adjustment** (default: -25 points). This doesn't hide the PR entirely — it just pushes it down the ranking below things that actually need the operator's attention.

### Data Sources

Committer list: fetched from the project's COMMITTERS file or the GitHub team with commit access. Cached and refreshed weekly. Dual-path (API: org members endpoint; scrape: parse the committers page on the project website).

Review activity: fetched as part of normal PR polling. Check the `reviews` endpoint for comments from known committers within the recency window.

### Configuration

```yaml
# In project config
committer_deranking:
  enabled: true
  committer_list_source: github-team  # github-team | file | url
  # github-team: reads org team with push/maintain access
  # file: reads from a local file (e.g., COMMITTERS.md parsed)
  # url: fetches and parses a URL (e.g., spark.apache.org/committers.html)
  recency_hours: 48
  score_adjustment: -25
  # Never derank if:
  # - PR is in operator's watch_paths
  # - Operator is @-mentioned
  # - Operator authored the PR
  # - PR is from a collaborator (auto-detected or manual)
```

### Version Targeting

v1 — the signal is straightforward and uses data we already fetch.

---

## 2.8 Workspace Mode (Project Filtering)

### Problem

The operator works in different contexts at different times: at work they can only review Spark and Snowflake connector PRs (not FHI); outside work they want FHI and personal repos (not necessarily Spark). The dashboard and digest need to respect these boundaries.

### Design

Workspaces are named sets of projects. The operator defines them in config:

```yaml
# In config.yaml
workspaces:
  work:
    projects: [apache-spark, snowflake-spark-connector, spark-testing-base]
    description: "Snowflake / ASF work"
  personal:
    projects: [fight-health-insurance, holdensmagicalunicorn, spp-matrix]
    description: "Side projects & personal infra"
  all:
    projects: "*"  # special: includes everything
    description: "Everything"
```

### Dashboard Integration

A workspace selector in the dashboard header. Persisted in a cookie / local session (not in SQLite — this is a UI preference, not agent state). Switching workspaces filters:

- Inbox: only shows PRs from projects in the active workspace
- Queue tabs: filtered
- Merge queue: filtered
- Stats: scoped to workspace

Keyboard shortcut: `W` cycles through workspaces.

### Digest Integration

The daily email digest can be configured per-workspace or show all:

```yaml
email_digest:
  workspace: all  # or "work" or "personal"
  # If "all": sections are grouped by workspace
  # If specific: only that workspace's PRs appear
```

Alternatively, send separate digests for different workspaces at different times (e.g., work digest at 7am, personal digest at 7pm):

```yaml
email_digest:
  schedules:
    - workspace: work
      send_at: "07:00"
    - workspace: personal
      send_at: "19:00"
```

### Version Targeting

v1 — simple to implement, high daily-use value.

---

## 2.9 Custom Scoring Functions (LLM-Generated)

### Problem

The default interest scoring signals (path overlap, collaborators, blame, etc.) are generic. Every project has domain-specific patterns that matter — memory management in Spark, HIPAA-adjacent changes in FHI, serialization quirks in the Snowflake connector. Hardcoding these as config options doesn't scale. The operator knows what matters; they just need a way to express it.

### Design: Operator Describes, LLM Generates

The operator writes a natural-language description of what they care about. The agent uses an LLM to generate a scoring function from this description, which is then applied to PR diffs during interest scoring.

```yaml
# In project config
custom_scoring:
  enabled: true
  description: |
    Things I especially care about in Spark PRs:
    
    - Memory-related changes: Flag PRs touching memory limits (e.g., 
      RLIMIT_AS), serialization buffers, or OOM prevention logic. These
      are subtle and often cause production issues.
    
    - Language bridge issues: Higher scores for PRs that change Scala 
      APIs without updating the corresponding Python/PySpark implementations.
      This is a common source of user-facing bugs.
    
    - Environment-specific infrastructure: PRs touching YARN or Kubernetes 
      (K8s) integration, as these are critical for distributed stability.
    
    - UDF/lambda transpilation: Anything touching the Python-to-Catalyst 
      expression conversion pipeline. I'm the primary author and reviewer.
    
    - Deprecation without migration: PRs that deprecate APIs without 
      providing a migration path or updating the migration guide.
  
  # Generated scoring function is cached here
  # Regenerate with: review-agent regenerate-scoring --project spark
  generated_function_file: ~/.review-agent/projects/spark.scoring_fn.py
  
  # Weight applied to custom scoring output (0-30 points)
  max_boost: 30
```

### Generation Pipeline

```bash
review-agent regenerate-scoring --project spark

  Reading custom scoring description from spark.yaml...
  
  Generating scoring function using primary backend...
  
  Generated function checks for:
    ✓ Memory keywords (RLIMIT_AS, OOM, OutOfMemory, serialization, buffer)
    ✓ Scala-without-Python detection (changed .scala files in sql/ 
      without corresponding .py changes in pyspark/)
    ✓ YARN/K8s path detection (resource-managers/yarn/, kubernetes/)
    ✓ UDF transpilation paths (catalyst/expressions/Lambda*, 
      python/pyspark/sql/udf*)
    ✓ Deprecation-without-migration (@deprecated annotations without 
      migration-guide.md changes)
  
  Saved to: ~/.review-agent/projects/spark.scoring_fn.py
  
  Review the generated function? [Y/n]: y
  [opens in $EDITOR]
```

### Generated Function Format

The LLM generates a pure Python function with a fixed signature. No imports beyond stdlib — the function operates on the PR data model the agent already has.

```python
# ~/.review-agent/projects/spark.scoring_fn.py
# Auto-generated by franktheunicorn from operator description
# Regenerate with: review-agent regenerate-scoring --project spark
# 
# IMPORTANT: Review this function before enabling. The LLM generated it
# from your description, but it might miss edge cases or be over-broad.
# Edit freely — manual edits are preserved across regeneration if you
# use 'regenerate-scoring --preserve-edits'.

def custom_score(pr_data: dict) -> float:
    """
    Returns a score between 0.0 and 1.0 indicating how much this PR
    matches the operator's custom interest criteria.
    
    pr_data contains:
      - title: str
      - body: str
      - changed_files: list[str]  (file paths)
      - diff_text: str  (full unified diff)
      - labels: list[str]
      - author: str
    """
    score = 0.0
    files = pr_data.get("changed_files", [])
    diff = pr_data.get("diff_text", "").lower()
    
    # --- Memory-related changes ---
    memory_keywords = [
        "rlimit_as", "oom", "outofmemory", "memory_limit",
        "max_memory", "serialization", "serializerbuffer",
        "unsafe.allocate", "directbytebuffer", "memorymanager"
    ]
    if any(kw in diff for kw in memory_keywords):
        score += 0.3
    
    # --- Language bridge: Scala changed without Python counterpart ---
    scala_sql_changed = any(
        f.endswith(".scala") and "sql/" in f for f in files
    )
    python_pyspark_changed = any(
        f.endswith(".py") and "pyspark/" in f for f in files
    )
    if scala_sql_changed and not python_pyspark_changed:
        score += 0.25
    
    # --- YARN / K8s infrastructure ---
    infra_paths = ["resource-managers/yarn/", "resource-managers/kubernetes/",
                    "k8s/", "yarn/"]
    if any(any(ip in f for ip in infra_paths) for f in files):
        score += 0.2
    
    # --- UDF / lambda transpilation ---
    udf_paths = ["catalyst/expressions/lambda", "pyspark/sql/udf",
                  "catalyst/expressions/pythonudf", "connect/common/src"]
    if any(any(up in f.lower() for up in udf_paths) for f in files):
        score += 0.35
    
    # --- Deprecation without migration guide ---
    has_deprecation = "@deprecated" in diff or "deprecationwarning" in diff
    has_migration_update = any("migration" in f.lower() for f in files)
    if has_deprecation and not has_migration_update:
        score += 0.15
    
    return min(score, 1.0)  # cap at 1.0
```

### Integration with Interest Scoring

The custom scoring function runs during the scoring phase, after the standard signals:

```python
# In the scoring pipeline
base_score = compute_standard_signals(pr)  # 0-100 from standard signals

if project.custom_scoring.enabled:
    custom_fn = load_custom_scoring_function(project)
    custom_result = custom_fn(pr.to_scoring_dict())  # 0.0 to 1.0
    custom_boost = custom_result * project.custom_scoring.max_boost  # 0 to 30
    total_score = min(base_score + custom_boost, 100)
else:
    total_score = base_score
```

### Safety

The generated function runs in a restricted environment:

- **No imports.** The function signature is enforced; only stdlib operations on the input dict are allowed.
- **Timeout.** 1-second execution limit per PR. If the function hangs or is expensive, it's killed and returns 0.
- **Sandboxed execution.** Uses `RestrictedPython` or `exec()` with a stripped `__builtins__` — no file I/O, no network, no subprocess.
- **Human review required.** The generated function is saved to a file and the operator is prompted to review it before it's activated. `regenerate-scoring` never auto-activates.
- **Manual edits preserved.** `regenerate-scoring --preserve-edits` diffs the new generation against the current file and merges, preserving manual tweaks.

### FHI Example

```yaml
# In fhi.yaml
custom_scoring:
  enabled: true
  description: |
    What I care about in FHI PRs:
    
    - Any change touching user data models (Patient, Appeal, 
      InsuranceClaim) — these are HIPAA-adjacent.
    - Changes to external API integrations (insurance company APIs,
      fax services) — these break silently.
    - New dependencies — each one is an attack surface.
    - Changes to the appeal letter generation pipeline — this is
      the core product, regressions are user-facing.
    - Missing Django migrations when models change.
```

### Version Targeting

v1 — the generation is a one-time LLM call during project setup. The generated function is a static `.py` file. Simple to implement, high value. The LLM call uses the primary backend (Sonnet), which is already configured.

---

## 3. Review Findings (Core Abstraction)

### 3.1 Why a Separate Abstraction

A "review finding" is not the same as a GitHub comment. The agent produces findings; some become GitHub comments, some don't. The finding abstraction enables: deduplication across backends (one finding may be flagged by CodeRabbit and the agent), editing and batching before posting, reasoning traces (why was this flagged?), and feedback loop tracking independent of GitHub state.

### 3.2 Finding Schema

```
ReviewFinding:
  id
  pr
  file_path
  line_range (start, end)
  category: correctness | style | security | test-coverage |
            architectural | naming | suggested-change | moderation
  severity: critical | important | nit | informational
  body (the actual review text)
  suggestion_diff (for suggested changes, null otherwise)
  reasoning_trace (why the agent flagged this)
  sources: [agent-primary, agent-fast, coderabbit, ...]
  confidence: high | medium | low
  tone_guard_applied: bool
  status: pending | approved | edited | rejected | posted | recalled
  operator_edit (null or edited text)
  rejection_reason (null or text)
  github_comment_id (null until posted)
```

Findings are the unit of work in the dashboard. The operator approves, edits, or rejects *findings*, which are then translated into GitHub review comments at posting time.

### 3.3 Deduplication

When multiple backends or CodeRabbit (v1.5) flag the same file/line region:
- Merge into a single finding with multiple `sources`
- Use the most detailed/useful body
- Preserve all reasoning traces
- In v1 (no CodeRabbit), this mainly deduplicates between the fast and primary backends

---

## 3.5 Direct Agent Feedback Channel (v1.25)

### Problem

When a PR was generated by Claude Code or Codex, leaving review comments on GitHub is an indirect feedback path. The AI agent has to poll for comments, parse them, and re-enter its own workflow. If the operator has an active Claude Code session (or Codex session) that generated the PR, it's faster and more effective to send feedback directly to that session.

### How It Works

AI-generated PRs often include a session link or identifier in the PR description:

```markdown
Generated by Claude Code
Session: https://claude.ai/code/session/abc123
```

Or for Codex:

```markdown
Generated by Codex
Task ID: task_xyz789
```

The agent parses these from the PR description during ingestion. When present, the PR detail view in the dashboard shows a **"Send Feedback to Session"** button alongside the existing "Send Feedback to Agent" (GitHub comment) button.

### Feedback Flow

```
Operator reviews AI-generated PR in dashboard
  → Approves/edits/rejects findings as usual
  → Clicks "Send Feedback to Session"
  │
  ├─► Claude Code session link present?
  │     → Open the session URL with feedback pre-populated
  │     → Format: structured markdown with:
  │         - Assessment (good / needs work / reject)
  │         - Specific file/line feedback from findings
  │         - Test verification results
  │         - Suggested changes
  │     → Operator can edit before sending
  │
  ├─► Codex task ID present?
  │     → Call Codex API with feedback payload
  │     → Format: structured JSON matching Codex's feedback schema
  │
  └─► Neither present?
        → Fall back to "Send Feedback to Agent" (GitHub comment)
```

### Session Link Detection

```python
# Patterns to detect in PR description
SESSION_PATTERNS = [
    # Claude Code
    (r'Session:\s*(https://claude\.ai/code/session/\S+)', 'claude-code'),
    (r'claude[- ]code.*session[: ]+(\S+)', 'claude-code'),
    # Codex
    (r'Task ID:\s*(task_\S+)', 'codex'),
    (r'codex.*task[: ]+(\S+)', 'codex'),
    # Generic: any URL in a "Generated by" block
    (r'Generated by.*\n.*?(https://\S+)', 'generic'),
]
```

### Configuration

```yaml
# In config.yaml
agent_feedback:
  direct_session_enabled: true
  # Which agents support direct feedback
  supported_agents:
    - name: claude-code
      session_pattern: 'Session:\s*(https://claude\.ai/code/session/\S+)'
      feedback_method: url-open  # opens URL in browser with feedback
    - name: codex
      session_pattern: 'Task ID:\s*(task_\S+)'
      feedback_method: api  # calls API endpoint
      api_endpoint_env: CODEX_FEEDBACK_API
```

### Version Targeting

v1.25 — needs the base review pipeline (v1) working first, plus some API investigation for Codex. Claude Code URL-open is simpler to ship.

---

## 4. Tone Guard

### 4.1 Philosophy

The goal is avoiding sounding like a jerk, not producing corporate fluff. Tone Guard is a **transformation**, not a gate — it rewrites comments for constructiveness rather than suppressing them.

**Preserve:** directness, technical precision, actionable suggestions.
**Remove:** unnecessary abrasiveness, pedantic corrections, snarky phrasing, condescension.

### 4.2 Dual-Stage Application

**Stage 1: Training data curation (during voice bootstrapping).**
When the Comment Curator CLI processes scraped review comments, it flags comments with problematic tone. The operator can exclude, edit, or include-with-note. This prevents bad historical tone from contaminating the voice dataset.

**Stage 2: Draft-time rewriting.**
After the agent generates a draft finding, the primary backend applies the project's `tone.objective` as a rewriting pass. The original text is preserved in `reasoning_trace`; the rewritten version becomes the `body`.

### 4.3 Per-Project Configuration

```yaml
tone:
  objective: |
    Preserve directness and technical precision. Remove snark.
    For ASF projects: remember this is volunteer work.
  new_contributor_addendum: |
    Be extra welcoming. Include a note about where to find
    project conventions. Don't pile on nits — focus on the
    most important 2-3 issues.
```

The `new_contributor_addendum` is applied when the `new_contributor` flag is set on a PR. It's appended to the tone objective for that review pass.

---

## 5. Learning from Corrections (Three-Tier System)

The previous design treated learning as: anti-pattern list (v1) then fine-tuning (v2). This was too binary. There are three distinct tiers, each with different data requirements, complexity, and latency.

### Tier 1: Context Storage (v1)

**What:** Anti-pattern list + curated voice examples + operator edit history. All stored as structured data in files and SQLite.

**How it improves the agent:** Direct injection into LLM prompts. Anti-patterns say "don't do this." Voice examples say "do it like this." Edit history provides few-shot examples of corrections.

**Latency to improve:** Immediate. A rejected comment can be turned into an anti-pattern that same session. Next review cycle already benefits.

**Data requirement:** 1+ rejection to start learning.

**Implementation details already covered in §5 of base doc.** One addition: the edit history should be structured as (original, corrected) pairs, with metadata:

```jsonl
{"project": "apache-spark", "category": "correctness", "original": "The null check here is wrong.", "corrected": "Use isNullAt(idx) instead of == null to handle Spark's internal null representation — see ConnectPlanner.scala:142.", "edit_type": "added_context", "pr": 54102, "timestamp": "2026-03-27T10:15:00Z"}
{"project": "apache-spark", "category": "style-nit", "original": "This variable name is unclear.", "corrected": null, "edit_type": "rejected", "rejection_reason": "too nitpicky for Spark", "pr": 54103, "timestamp": "2026-03-27T10:20:00Z"}
```

The LLM prompt includes the N most recent (original, corrected) pairs as few-shot correction examples, in addition to the anti-pattern list. This gives the model concrete examples of how its output should differ.

### Tier 2: Bayesian Rejection Model (v1.75)

**What:** A lightweight statistical model trained on rejection patterns. Predicts the probability that a candidate finding will be rejected *before* showing it to the operator.

**Why not just anti-patterns?** Anti-patterns are precise but brittle — they match exact categories and patterns. A Bayesian model captures fuzzier signals: "findings about import ordering in test files for ASF projects have an 85% rejection rate" even if no single anti-pattern entry captures that.

**How it works:**

```
Features for each candidate finding:
  - project_type (asf / personal / org)
  - file_path pattern (test file? config? core code?)
  - category (correctness, style-nit, naming, etc.)
  - severity (critical, important, nit, informational)
  - file_language (python, scala, java, etc.)
  - is_new_contributor (bool)
  - is_ai_generated_pr (bool)
  - diff_size (lines changed)
  - operator's historical approval rate for this (project, category) pair

Output:
  - P(rejection) — probability the operator will reject this finding
```

**Training data:** The same operator action history used for Tier 1 and Tier 3. Each approved/rejected finding becomes a training example. Features are cheap to compute; no LLM calls needed.

**Model:** Naive Bayes or logistic regression. Scikit-learn. Fits in memory, trains in seconds, serializes to a small pickle file. No GPU needed.

```python
from sklearn.naive_bayes import MultinomialNB
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction import DictVectorizer

class RejectionPredictor:
    def __init__(self):
        self.model = Pipeline([
            ('vectorizer', DictVectorizer(sparse=True)),
            ('classifier', MultinomialNB(alpha=1.0))
        ])
    
    def train(self, findings: list[dict], labels: list[bool]):
        """labels: True = rejected, False = approved/edited"""
        features = [self._extract_features(f) for f in findings]
        self.model.fit(features, labels)
    
    def predict_rejection(self, finding: dict) -> float:
        """Returns P(rejection) between 0 and 1."""
        features = self._extract_features(finding)
        return self.model.predict_proba([features])[0][1]
    
    def _extract_features(self, finding: dict) -> dict:
        return {
            'project_type': finding['project_type'],
            'category': finding['category'],
            'severity': finding['severity'],
            'file_ext': finding['file_path'].rsplit('.', 1)[-1],
            'is_test_file': 'test' in finding['file_path'].lower(),
            'is_new_contributor': finding.get('is_new_contributor', False),
            'is_ai_pr': finding.get('is_ai_generated', False),
        }
```

**Integration:**

Findings with `P(rejection) > 0.8` are auto-suppressed (not shown to operator by default, but visible in a "suppressed" section of the dashboard). Findings with `0.5 < P(rejection) < 0.8` are shown but flagged as "likely low-value." This reduces noise without losing signal.

**Retraining:** Automatic. After every 50 new operator actions, retrain the model (takes < 1 second). Store the model in `DATA_DIR/models/<owner>-<repo>/rejection_model.pkl` (where `DATA_DIR` defaults to `<project_root>/data/`, configurable via `FRANK_DATA_DIR`).

**Data requirement:** 50+ operator actions to start (enough for basic Bayesian estimation). Gets meaningfully better at 200+.

**Version targeting:** v1.75 — after the core feedback loop is working and generating data, before the expensive fine-tuning investment.

### Tier 3: Fine-Tuned Personal Model (v2)

**What:** Axolotl QLoRA fine-tune on an open base model, trained on the operator's accumulated action history. Already detailed in v3.1 §8.

**Default base model:** Must fit on a single 3090 (24GB VRAM) for QLoRA training and inference. Recommendations:

| Model | Size | VRAM (QLoRA training) | VRAM (inference, 4-bit) | Quality |
|-------|------|-----------------------|-------------------------|---------|
| **Qwen2.5-Coder-7B-Instruct** | 7B | ~12GB | ~5GB | Best code-specific option at 7B |
| **Mistral-7B-Instruct-v0.3** | 7B | ~12GB | ~5GB | Strong general + code |
| **CodeLlama-13B-Instruct** | 13B | ~18GB | ~8GB | Better quality, tighter fit |
| **Qwen2.5-Coder-14B-Instruct** | 14B | ~20GB | ~9GB | Best quality that fits |
| DeepSeek-Coder-V2-Lite (16B) | 16B | ~22GB | ~10GB | Pushes 3090 limits |

**Default: Qwen2.5-Coder-7B-Instruct.** Code-specialized, fast to train, fast to infer, leaves headroom on the 3090 for other work. Operators with more VRAM or multiple GPUs can configure larger models.

```yaml
# In config.yaml
fine_tuning:
  default_base_model: Qwen/Qwen2.5-Coder-7B-Instruct
  # Override per-project if desired:
  # base_model_override:
  #   apache-spark: Qwen/Qwen2.5-Coder-14B-Instruct
  quantization: qlora-4bit  # 4-bit for training, 4-bit for inference
  target_hardware: 3090     # informational, affects default configs
```

**Data requirement:** 200+ operator actions per project.

**When it makes sense:** The Bayesian model (Tier 2) handles "should I show this?" The fine-tuned model handles "what should the comment say?" They're complementary:

```
Candidate review region identified
  → Bayesian model: P(rejection) = 0.15 (worth commenting on)
  → Fine-tuned model generates draft comment in operator's voice
  → Tone Guard rewrites for constructiveness
  → Finding enters review queue
```

### Summary: Three-Tier Learning Timeline

```
Day 1 ──────── v1 ────────────── v1.75 ──────────── v2 ─────────
│                                  │                    │
│  Tier 1: Context Storage         │  Tier 2: Bayesian  │  Tier 3: Fine-tune
│  - Anti-pattern list             │  - Rejection model  │  - Personal model
│  - Voice examples                │  - Auto-suppress    │  - Operator's voice
│  - Edit history pairs            │  - 50+ actions      │  - 200+ actions
│  - 1+ rejection to start         │  - Trains in <1s    │  - Trains in hours
│  - Immediate effect              │  - sklearn, no GPU  │  - Axolotl, 3090
│                                  │  - .pkl file        │  - Ollama serving
│                                  │                    │
│  Injected into prompts ─────────►│  Pre-filters ──────►│  Generates drafts
```

---

## 6. Voice Bootstrapping (Comment Curator)

### 6.1 Philosophy

Raw scraping of historical review comments is a starting point, not a dataset. Historical comments contain tone issues, outdated conventions, and off-topic tangents. The Comment Curator CLI produces a curated dataset.

### 6.2 Pipeline

```bash
review-agent curate-voice --project spark

  1. Scrape last ~100 review comments per project from GitHub
  2. Classify each by category (correctness, style, architectural, etc.)
  3. Flag tone issues (abrasive, snarky, overly pedantic)
  4. Present to operator in interactive TUI:
     - Each comment shown with diff context
     - Actions: [include] [exclude] [edit] [skip]
     - Edits are stored as the "preferred" version
  5. Output: ~/.review-agent/voice/spark/voice_curated.jsonl
```

### 6.3 Decision Learning over Style Learning

The curated dataset emphasizes *what gets commented on* (decision patterns) over *how it sounds* (style). The voice examples teach the agent:

- What kinds of issues warrant a comment vs. being ignored
- How detailed the comment should be for this project
- When to suggest a fix vs. ask a question vs. flag for discussion

Style is handled by the project's `review_context` and `tone.objective`. The voice dataset handles judgment.

### 6.4 Dataset Structure

```jsonl
{"project": "apache-spark", "category": "correctness", "decision": "comment",
 "diff_context": "...", "comment": "Use isNullAt(idx) instead of == null...",
 "curated": true, "tone_flagged": false}
{"project": "apache-spark", "category": "style-nit", "decision": "ignore",
 "diff_context": "...", "comment": null,
 "note": "operator excluded — too nitpicky for Spark"}
```

---

## 7. Data Access Layer (Dual-Path: API + Scrape)

### 7.1 Philosophy

Every external data source has two access paths: a clean API client and a scrape fallback. Both paths implement the same interface, return the same data types, and are thoroughly tested. The scrape path exists because APIs have rate limits, APIs go down, APIs change pricing, and some data is only available on the web.

**This is not "scraping as a hack."** Both paths are first-class, maintained, and tested. The system selects which path to use based on: rate limit headroom (prefer API when possible), availability (fall back to scrape on API errors), and data availability (some data only exists in web UI).

### 7.2 Architecture

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

class DataFetcher(ABC):
    """Base interface for all data access. Every source implements both paths."""
    
    @abstractmethod
    async def fetch_via_api(self, **kwargs) -> FetchResult:
        """Primary path: structured API call."""
        ...
    
    @abstractmethod
    async def fetch_via_scrape(self, **kwargs) -> FetchResult:
        """Fallback path: HTML parsing."""
        ...
    
    async def fetch(self, **kwargs) -> FetchResult:
        """Unified entry point. Tries API first, falls back to scrape."""
        try:
            if self.rate_limiter.has_headroom("api"):
                return await self.fetch_via_api(**kwargs)
            else:
                return await self.fetch_via_scrape(**kwargs)
        except APIError:
            return await self.fetch_via_scrape(**kwargs)
```

### 7.3 Source Matrix

| Data Source | API Path | Scrape Path | Cache Strategy |
|-------------|----------|-------------|----------------|
| **GitHub PRs** | REST API v3 `GET /repos/{o}/{r}/pulls` | Parse `github.com/{o}/{r}/pulls` | Poll-based; ETag/If-Modified-Since |
| **GitHub PR diff** | REST API `GET /repos/{o}/{r}/pulls/{n}` (Accept: diff) | Parse PR files tab HTML | Cache per PR + head commit |
| **GitHub reviews/comments** | REST API `GET /repos/{o}/{r}/pulls/{n}/reviews` | Parse PR conversation HTML | Cache per PR, invalidate on update |
| **GitHub blame** | GraphQL `blame` field OR REST blame endpoint | Parse blame HTML view | Cache per file + commit hash |
| **GitHub user info** | REST API `GET /users/{u}` | Parse profile page | Cache indefinitely |
| **Git log (local)** | `git log` on cloned repo | N/A (local only) | Refresh on fetch |
| **JIRA tickets** (v1.5) | REST API `GET /rest/api/2/issue/{key}` | Parse JIRA issue page HTML | Lazy fetch + indefinite cache |
| **Mailing list archives** (v1.5) | Apache lists.apache.org search API | Parse pipermail/mbox archives | Cache search results per query |
| **Discord** (v1.5) | Bot API: search messages | N/A (API-only; no public web scraping) | Cache per search query |
| **Discourse** (v1.5) | Discourse search API `/search.json` | Parse search results HTML | Cache per query |
| **CodeRabbit** (v1.5) | CLI invocation (structured JSON output) | N/A (CLI-only) | Cache per PR + head commit |

### 7.4 Implementation Guidelines

**Scraping stack:** `httpx` for async HTTP, `selectolax` (or `BeautifulSoup4`) for HTML parsing. Prefer `selectolax` for performance — it's a Modest-based parser that's 10-30x faster than BS4.

**Testing:** Every data source has parallel test suites:

```
tests/
  data_access/
    github/
      test_prs_api.py          # tests API path with VCR cassettes
      test_prs_scrape.py       # tests scrape path with saved HTML fixtures
      test_prs_integration.py  # tests unified fetch() with both paths
      fixtures/
        pr_list_api_response.json
        pr_list_page.html
    jira/
      test_tickets_api.py
      test_tickets_scrape.py
      fixtures/
        ticket_api_response.json
        ticket_page.html
    blame/
      test_blame_api.py
      test_blame_scrape.py
      fixtures/
        blame_graphql_response.json
        blame_page.html
```

**Fixture generation:** A helper script records live API responses and HTML pages as test fixtures:

```bash
review-agent record-fixtures --source github --project spark
  # Fetches real data via both API and scrape
  # Saves as test fixtures
  # Redacts tokens/personal data
```

**Contract testing:** Both paths must produce identical output for the same input. A shared test suite runs both implementations against the same fixtures and asserts output equality:

```python
@pytest.mark.parametrize("fetcher", [GitHubPRFetcherAPI(), GitHubPRFetcherScrape()])
def test_pr_list_returns_consistent_schema(fetcher, mock_data):
    result = fetcher.fetch(repo="apache/spark", state="open")
    assert isinstance(result, list)
    assert all(isinstance(pr, PRSummary) for pr in result)
    assert result[0].number == 54102
    assert result[0].author == "huaxin-gao"
```

### 7.5 Rate Limiting

**Library:** `pyrate-limiter` with SQLite bucket backend (fits local-first philosophy — rate limit state persists across restarts).

```python
from pyrate_limiter import Duration, Limiter, RequestRate, SQLiteBucket

# GitHub: 5000 req/hr for authenticated, 60/hr unauthenticated
github_limiter = Limiter(
    RequestRate(4500, Duration.HOUR),  # leave 10% headroom
    bucket_class=SQLiteBucket,
    bucket_kwargs={"path": "~/.review-agent/rate-limits.sqlite"}
)

# Anthropic: model-specific, configured from env
anthropic_limiter = Limiter(
    RequestRate(
        int(os.getenv("ANTHROPIC_RPM", "50")),
        Duration.MINUTE
    ),
    bucket_class=SQLiteBucket,
    bucket_kwargs={"path": "~/.review-agent/rate-limits.sqlite"}
)
```

**Adaptive GitHub rate limiting:** In addition to the static limiter, the GitHub client reads `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers from every response and adjusts behavior:

```python
class AdaptiveGitHubClient:
    async def request(self, method, url, **kwargs):
        # Check static rate limiter first
        await self.static_limiter.try_acquire("github-api")
        
        response = await self.http.request(method, url, **kwargs)
        
        # Read GitHub's rate limit headers
        remaining = int(response.headers.get("X-RateLimit-Remaining", 5000))
        reset_at = int(response.headers.get("X-RateLimit-Reset", 0))
        
        if remaining < 100:
            # Getting low — switch to scrape path for non-critical requests
            self.prefer_scrape = True
            self.scrape_until = reset_at
        
        if remaining == 0:
            # Exhausted — block until reset
            wait = max(0, reset_at - time.time())
            logger.warning(f"GitHub API exhausted, waiting {wait:.0f}s")
            await asyncio.sleep(wait)
        
        return response
```

**Rate limit state is in SQLite.** This means: it persists across worker restarts, it's part of the backup (`~/.review-agent/`), and the dashboard can show current rate limit status.

**Separate limiters per service:** GitHub API, GitHub scrape (be polite — 1 req/s), Anthropic API, JIRA API, mailing list archives. Each configured independently.

---

## 8. Multi-Backend Review Pipeline

### 7.1 v1 Pipeline

```
PR arrives → Worker picks up (priority: is_operator_pr first, then score)
  │
  ├─► Moderation flag assessment
  │     └─► Route to appropriate queue
  │
  ├─► Agent review pipeline:
  │     ├─ Fast backend: style nits, commit message, label suggestion
  │     ├─ Primary backend: line-level findings, PR summary,
  │     │    suggested changes, tone guard rewrite
  │     └─ Reasoning backend: architectural concerns, correctness
  │          (only if score > 70 or PR touches core APIs)
  │     │
  │     └─► Anti-pattern filter:
  │           - Suppress findings matching anti-pattern entries
  │           │
  │           └─► Dedup (across backends)
  │                 │
  │                 └─► Review Findings → Dashboard Queue
  │
  ├─► Differential test run (async, see §8)
  │     └─► Results attached to relevant findings when ready
  │
  └─► "No test coverage" assessment
        └─► Dashboard badge + draft finding if applicable
```

### 7.2 v1.5 Additions

- CodeRabbit CLI scan → structured JSON → dedup with agent findings
- Community context search (blocking, with timeouts) → injected into LLM context
- JIRA lazy-fetch → injected into LLM context
- Confidence gating for auto-post (triple gate)

### 7.3 Context Window Construction

For each LLM call:

1. **Project profile** — `review_context`, `tone.objective`, references
2. **Anti-patterns** — injected as "do NOT produce these"
3. **Voice examples** — curated comments (few-shot)
4. **PR metadata** — title, body, labels, author
5. **Diff** — full for small PRs; chunked by file for large
6. **Community context** (v1.5) — relevant threads from mailing list/Discord
7. **JIRA ticket** (v1.5) — summary + recent comments

### 7.4 Suggested Changes

For trivially fixable issues, the agent generates GitHub `suggestion` blocks. Configurable per-project: `suggestion-block` (PR author clicks apply) or `direct-commit` (agent commits the fix). Both go through the review queue in v1.

---

## 9. Differential Test Verification

> **Operator guide:** for the per-project YAML schema, the three image-source
> modes (prebuilt / Dockerfile / auto-build), and troubleshooting, see
> [`test-runner.md`](test-runner.md). The sections below describe the design.

### 9.1 Test Identification (Three Sources)

1. **Diff analysis** (deterministic): new/modified test files from `git diff`
2. **PR description NLP** (LLM-assisted): primary backend extracts test references
3. **Explicit callouts**: tests tagged in PR template

Union of all three defines scope. Only these tests run — not the full suite.

### 9.2 Execution Flow

```
Container 1 (PR branch): checkout PR → setup → run scoped tests
Container 2 (base + cherry-picked tests): checkout base → cherry-pick
  test files only → run same tests

Differential analysis:
  GOOD:    pass on PR, fail on base (test validates the change)
  SUSPECT: pass on both (test doesn't catch the change)
  BROKEN:  fail on both (flaky or broken)
  INFRA:   base errors on import/setup (inconclusive — not assertion failure)
```

### 9.3 When Tests Run

| Trigger | Action |
|---------|--------|
| PR has new/modified test files | Full differential |
| PR is AI-generated | Full differential (mandatory) |
| PR has no test changes, score ≥ 40 | No test run; flag "no test coverage" (badge + draft finding) |
| PR has no test changes, score < 40 | No run, no flag |
| Operator requests via dashboard | Full differential |

### 9.4 Resource Tiers

| Tier | Resources | Max Runtime |
|------|-----------|-------------|
| `heavy` | 8 CPU, 16GB | 45 min |
| `standard` | 4 CPU, 8GB | 15 min |
| `light` | 2 CPU, 4GB | 5 min |

Docker containers in v1. K8s Jobs in v2.

---

## 9.5 Container Security Model

### Problem

The agent executes dynamically generated test commands inside containers. A compromised or malicious PR could craft test code that escapes the container. This is the biggest security surface.

### Principles

1. **All dynamic code execution happens in rootless Docker.** Never on the host. Never in a privileged container.
2. **No Docker-in-Docker for the agent itself.** The agent's own containers (web, worker) don't need Docker socket access. Test execution uses a separate, sandboxed container runtime.
3. **Test containers are ephemeral, network-isolated, and resource-capped.** They can't reach the internet, can't access the host filesystem (except the cloned repo mounted read-only), and have hard CPU/memory/time limits.

### Architecture

```
┌─────────────────────────────────────────┐
│  Host / Docker Compose                  │
│                                         │
│  ┌─────────┐  ┌─────────┐              │
│  │   web   │  │ worker  │              │
│  │ (Django) │  │ (polls, │              │
│  │         │  │  reviews)│              │
│  └─────────┘  └────┬────┘              │
│                     │                    │
│                     │ spawns via Docker API
│                     │ (rootless, --security-opt=no-new-privileges)
│                     ▼                    │
│  ┌─────────────────────────────────┐    │
│  │  Test Container (ephemeral)     │    │
│  │  - rootless                     │    │
│  │  - no network (--network=none)  │    │
│  │  - read-only repo mount         │    │
│  │  - CPU/mem/time limits          │    │
│  │  - no Docker socket access      │    │
│  │  - dropped capabilities         │    │
│  └─────────────────────────────────┘    │
└─────────────────────────────────────────┘
```

### Docker Socket Access

The worker container needs access to the Docker socket to spawn test containers. This is the one privileged operation. Options:

1. **Rootless Docker:** Run the Docker daemon itself in rootless mode. Test containers inherit rootless isolation.
2. **Docker socket proxy:** Use a socket proxy (e.g., `tecnativa/docker-socket-proxy`) that only allows container creation/removal, not host access.
3. **Firecracker (optional, see below):** For maximum isolation, use Firecracker microVMs instead of Docker containers.

### Docker Compose Configuration

```yaml
services:
  web:
    build: .
    command: gunicorn franktheunicorn.wsgi
    # NO Docker socket access
    
  worker:
    build: .
    command: review-agent worker start
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro  # read-only socket
    # Worker uses Docker API to spawn test containers only
    # Does NOT run arbitrary code itself
    
  # Optional: Docker socket proxy for tighter control
  # docker-proxy:
  #   image: tecnativa/docker-socket-proxy
  #   environment:
  #     CONTAINERS: 1
  #     POST: 1
  #   volumes:
  #     - /var/run/docker.sock:/var/run/docker.sock
```

### Firecracker Support (Optional)

For operators who want microVM-level isolation, support Firecracker as an alternative container runtime:

```yaml
# In config.yaml
test_execution:
  runtime: docker  # docker | firecracker
  # docker: uses Docker API, rootless, --network=none
  # firecracker: uses Firecracker API, full microVM isolation
  firecracker:
    binary_path: /usr/local/bin/firecracker
    kernel_path: /opt/firecracker/vmlinux
    rootfs_base: /opt/firecracker/rootfs-base.ext4
```

The test execution layer uses a common interface — `TestRunner.run(image, commands, repo_path, timeout)` — with Docker and Firecracker implementations behind it. Same tests, same interface, different isolation levels.

---

## 9.6 Per-Project Container Images + Auto-Build

### Problem

Each project needs a container image with its dependencies to run tests. Requiring operators to pre-build and maintain images is friction. The agent should be able to auto-build an image from project requirements if one isn't specified.

### Design

```yaml
# In project config
tests:
  enabled: true
  container_image: null  # null = auto-build from project requirements
  # OR
  container_image: ghcr.io/apache/spark-test:latest  # explicit image
  
  # Auto-build config (used when container_image is null)
  auto_build:
    base_image: python:3.12-slim  # default base
    requirements_files:
      - requirements.txt
      - requirements-test.txt
    setup_commands:
      - pip install -e .
      - python manage.py migrate --run-syncdb
    # Built image tagged: franktheunicorn-test/<project-name>:latest
    # Rebuilt when requirements files change (hash-based)
```

### Auto-Build Flow

```
Test run requested for project with no explicit container_image
  │
  ├─► Check if auto-built image exists with current requirements hash
  │     YES → use it
  │     NO → build it:
  │       1. Generate Dockerfile from auto_build config
  │       2. docker build -t franktheunicorn-test/<project>:<hash>
  │       3. Cache the image locally
  │       4. Run tests in this image
  │
  └─► Requirements hash = sha256 of all requirements files concatenated
        Changes trigger rebuild on next test run
```

### Generated Dockerfile

```dockerfile
# Auto-generated by franktheunicorn for: fight-health-insurance
FROM python:3.12-slim

WORKDIR /workspace

# Copy requirements first for layer caching
COPY requirements.txt requirements-test.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-test.txt

# Setup commands
RUN python manage.py migrate --run-syncdb

# Repo will be mounted at /workspace at runtime (read-only)
```

### For Projects Without Python

The `base_image` and setup commands are fully configurable. A JVM project might use:

```yaml
auto_build:
  base_image: eclipse-temurin:17-jdk
  setup_commands:
    - ./build/mvn package -DskipTests -pl core,sql
```

---

## 10. Fine-Tuning with Axolotl

### 10.1 Prerequisites (What Must Be True Before Fine-Tuning)

Fine-tuning is explicitly deferred until:

1. **200+ operator actions** accumulated per project (approved, edited, rejected)
2. **Anti-pattern list is stable** — new entries less than 1/week (system has learned the obvious stuff)
3. **Approval rate ≥ 70%** — the base LLM + anti-patterns + voice examples are producing mostly-good output
4. **Operator explicitly triggers** — never auto-runs without opt-in

The anti-pattern list and operator edits are the *first-order* feedback system. Fine-tuning is a *second-order* optimization for capturing patterns that are hard to express as rules.

### 10.2 Training Data Pipeline

```bash
review-agent export-training-data --project spark --format axolotl

  Output: ~/.review-agent/training-data/spark/
    ├── train.jsonl          # 80% of data
    ├── eval.jsonl           # 20% held out
    ├── metadata.json        # dataset stats, date range, action breakdown
    └── axolotl_config.yaml  # generated config for this dataset
```

**Training data format (instruction-tuning):**

```jsonl
{
  "instruction": "You are reviewing a PR to Apache Spark. Project context:\n{review_context}\n\nAnti-patterns to avoid:\n{anti_patterns}\n\nReview the following diff and produce review comments.",
  "input": "File: python/pyspark/sql/connect/client.py\nDiff:\n```\n- if val == null:\n+ if val is None:\n```\nPR title: Fix null handling in Connect client\nPR body: Fixes SPARK-12345...",
  "output": "The fix is correct for the Python side, but the Connect protocol serializes nulls differently than local mode. You'll want to verify this works with `isNullAt(idx)` on the JVM side too — see `ConnectPlanner.scala:142` for the deserialization path."
}
```

**Negative examples (DPO-style, optional):**

```jsonl
{
  "instruction": "...",
  "input": "...",
  "chosen": "Use isNullAt(idx) instead of == null to handle Spark's internal null representation correctly.",
  "rejected": "This null check is wrong. You should know that == null doesn't work in Spark. Fix it."
}
```

The `chosen` response comes from operator edits (the version they actually posted). The `rejected` response is the original agent output that was edited. Tone Guard corrections are a rich source of DPO pairs.

### 10.3 Axolotl Configuration

Generated automatically by `review-agent export-training-data`:

```yaml
# ~/.review-agent/training-data/spark/axolotl_config.yaml
base_model: mistralai/Mistral-7B-v0.3
model_type: MistralForCausalLM

load_in_8bit: false
load_in_4bit: true  # QLoRA

adapter: qlora
lora_r: 32
lora_alpha: 64
lora_dropout: 0.05
lora_target_modules:
  - q_proj
  - k_proj
  - v_proj
  - o_proj
  - gate_proj
  - up_proj
  - down_proj

datasets:
  - path: train.jsonl
    type: alpaca
    
dataset_prepared_path: ./prepared

val_set_size: 0  # we use a separate eval.jsonl
output_dir: ./output

sequence_len: 4096
sample_packing: true

micro_batch_size: 2
gradient_accumulation_steps: 4
num_epochs: 3
learning_rate: 2e-4
lr_scheduler: cosine
warmup_steps: 10

bf16: auto
tf32: true

logging_steps: 10
save_strategy: epoch
eval_strategy: epoch

# Weights & Biases (optional)
# wandb_project: franktheunicorn-finetune
# wandb_run_id: spark-v1
```

### 10.4 Training Execution

```bash
review-agent fine-tune --project spark [--base-model mistralai/Mistral-7B-v0.3]

  Steps:
  1. Export training data (if not already exported)
  2. Validate dataset: min 200 examples, action distribution check
  3. Generate Axolotl config from template + dataset stats
  4. Run Axolotl training:
     - Local GPU: direct invocation
     - Remote: submit to configured endpoint (Modal, RunPod, Lambda)
     - Docker: `docker run --gpus all axolotl ...` (works with docker compose)
  5. Evaluate on held-out set:
     - Generate review comments for eval PRs
     - Compare against operator's actual comments
     - Compute: ROUGE-L, category accuracy, tone score
  6. If eval passes threshold:
     - Save adapter to ~/.review-agent/models/spark/v{N}/
     - Update project config to reference new model
     - Log training metadata for reproducibility
  7. Notify operator (email or dashboard notification)
```

### 10.5 Evaluation Metrics

| Metric | Target | What It Measures |
|--------|--------|-----------------|
| Category accuracy | ≥ 80% | Does the model flag the right *type* of issue? |
| ROUGE-L vs. operator comments | ≥ 0.35 | Textual overlap with what the operator actually wrote |
| Tone score | ≥ 0.8 | Automated tone assessment (does it pass Tone Guard?) |
| Anti-pattern violation rate | ≤ 5% | How often does the model produce comments matching anti-patterns? |
| False positive rate | ≤ 20% | How often does the model comment on code that doesn't need it? |

If evaluation fails, the model is not deployed and the operator is notified with the metrics that failed.

### 10.6 Serving

The fine-tuned model needs an inference endpoint. Options, in order of preference for local-first:

| Option | Latency | Cost | Setup | Local-First? |
|--------|---------|------|-------|-------------|
| **Ollama** | Low | Free (GPU needed) | `ollama create` from GGUF | ✅ Yes |
| **vLLM** | Low | Free (GPU needed) | `vllm serve --model ./adapter` | ✅ Yes |
| **llama.cpp** | Low | Free (CPU ok for 7B) | `llama-server -m model.gguf` | ✅ Yes |
| **Modal** | Medium | Pay per call | `modal deploy` | ❌ No |
| **RunPod** | Medium | Pay per hour | Serverless endpoint | ❌ No |
| **Together.ai** | Medium | Pay per token | Upload model | ❌ No |

For local-first, Ollama is the recommended default. It handles model loading, GGUF quantization, and provides an OpenAI-compatible API that the agent can call just like any other backend.

```yaml
# In project config after fine-tuning:
fine_tuned_model:
  enabled: true
  provider: ollama  # ollama | vllm | llama-cpp | modal | runpod | together
  model: franktheunicorn-spark-v1  # Ollama model name
  endpoint: http://localhost:11434  # default Ollama endpoint
  slot: first-pass  # first-pass | fast | primary | reasoning
  refine_with: primary  # which backend refines the first-pass output
```

### 10.7 Model Lifecycle

```
v1 (no model): anti-patterns + voice examples → LLM generates drafts
  ↓ (200+ operator actions accumulated)
v2 (first fine-tune): train on accumulated data → deploy → evaluate
  ↓ (model in production, new actions accumulating)
v3+ (retrain): monthly or on-demand, incorporating new actions
  ↓ (model improves with each cycle)
```

Each model version is saved with its training data, config, and eval metrics. The operator can roll back to any previous version or disable fine-tuning entirely.

---

## 10.8 Fine-Tuning: Dataset & Learning Patterns

### 10.8 Dataset: Voice-Curated JSONL as Foundation

The fine-tuning dataset is built on top of the persistent `voice_curated.jsonl` generated by the Comment Curator CLI (§6). This is the canonical dataset — it's been human-reviewed for tone and relevance. The fine-tuning pipeline never trains on raw scraped comments; everything goes through curation first.

Dataset composition:

```
voice_curated.jsonl          (base: curated historical comments)
  + approved_findings.jsonl  (ongoing: findings approved as-is from dashboard)
  + edited_findings.jsonl    (ongoing: operator-corrected findings as preference pairs)
  + rejected_findings.jsonl  (ongoing: negative examples)
  = training_dataset.jsonl   (merged, deduplicated, split into train/eval)
```

### 10.9 Learning Patterns: Praise-Suggestion Structure

The model is trained on a specific comment structure that reflects good OSS review practice — not vague critiques, but constructive observations that acknowledge what works before suggesting improvements.

**Target structure: Praise-Suggestion**

```
"Great structure on the test coverage here. Consider renaming 
`processData` to `processPartitionData` for clarity — it's easy 
to confuse with the top-level `processData` in DataProcessor.scala."
```

**Not this:**

```
"This naming is confusing."
```

During training data preparation, the pipeline:

1. Classifies each comment's structure (praise-suggestion, direct-fix, question, flag-for-discussion, pure-praise, pure-critique)
2. Weights praise-suggestion and direct-fix patterns higher (2x weight in loss)
3. Downweights or filters pure-critique patterns (these are usually the tone-flagged ones from curation)
4. For edited findings where the operator added constructive framing, the (original → edited) pair teaches the model the transformation explicitly

**Axolotl config addition for weighted training:**

```yaml
# In axolotl_config.yaml
datasets:
  - path: training_dataset.jsonl
    type: alpaca
    # Sample weights are embedded in each JSONL row as "weight" field
    # praise-suggestion: 2.0, direct-fix: 2.0, question: 1.5, 
    # flag-for-discussion: 1.0, pure-critique: 0.5
```

### 10.10 Periodic Refresh: Incremental Dataset Growth

The training dataset grows automatically as the operator uses the dashboard. A cron-triggered process (or a check within `review-agent fine-tune`) handles this:

```
Periodic refresh flow (configurable: daily/weekly/monthly):

1. Query DB for new operator actions since last export
     - Approved findings → append to approved_findings.jsonl
     - Edited findings → append to edited_findings.jsonl (as preference pairs)
     - Rejected findings → append to rejected_findings.jsonl

2. Deduplicate against existing dataset (by finding ID)

3. If auto_schedule.enabled AND new_actions >= min_new_actions:
     - Merge all JSONL files → training_dataset.jsonl
     - Split train/eval
     - Trigger fine-tune run
     - Notify operator on completion
```

```yaml
# In config.yaml
fine_tuning:
  auto_schedule:
    enabled: false  # opt-in
    check_frequency: weekly  # daily | weekly | monthly
    min_new_actions: 50
    notify_on_completion: true
  
  # Incremental export runs more frequently than fine-tuning
  # to keep the dataset fresh even before a training run
  dataset_refresh:
    enabled: true  # always on — just appends to JSONL files
    frequency: daily
```

The key insight: dataset refresh (appending new actions to JSONL) is cheap and always-on. Fine-tuning (running Axolotl) is expensive and opt-in. The dataset is always ready; the operator trains when they want to.

---

## 11. Dashboard (Django)

### 9.1 Architecture

Django. Responsive (mobile-first). htmx for interactivity, minimal JS. SQLite default. Django admin enabled. No app-level auth (Tailscale is the auth layer).

**Worker model:** The dashboard reads from SQLite. The worker writes to it. The worker is a separate process — can be a daemon, a cron job, or both. It's resumable: safe to kill and restart at any time. No always-on requirement.

### 9.2 Views

**Inbox (default):** PRs ranked by interest score. Cards show repo, title, author, score, badges, queue membership. Filterable by project, queue, score range.

**Your PRs:** Operator's own PRs with new activity. Separate from the review inbox — this is shepherding, not reviewing. Shows: new reviewer comments (highlighted by recency), CI status with flaky-test detection, merge conflicts, staleness.

**Queue Tabs:**
- Review queue (default) — standard PR review
- AI-generated — PRs from bots/agents
- New contributors — first-time PR authors
- Consider closing — stale, low-context, unowned PRs
- Needs triage — low-context drive-bys

**PR Detail View:**
- AI-generated summary
- Diff viewer with flagged regions
- Review findings with Approve / Edit / Reject (not "comments" — findings)
- Suggested changes as mini-diffs
- Test verification results
- For AI-generated PRs: "Send Feedback to Agent" panel
- v1.5: community context, JIRA, CodeRabbit findings (collapsed)

**Anti-Pattern Manager:**
- Full CRUD for anti-pattern entries
- Per-project filtering
- "Last matched" timestamps
- Bulk actions (disable, delete stale entries)
- Visible and prominent from day one

**History & Stats:**
- Past reviews, searchable
- Approved vs. edited vs. rejected rates
- Cost tracking (LLM tokens, container minutes)
- Anti-pattern effectiveness (how many findings suppressed)

### 9.3 Keyboard-Driven Triage

| Key | Action |
|-----|--------|
| `j` / `k` | Navigate between findings |
| `a` | Approve |
| `e` | Edit inline |
| `r` | Reject (optional reason) |
| `n` / `p` | Next / previous PR |
| `s` | Post all approved as single GitHub review |
| `x` | Expand/collapse diff context |
| `A` | Approve all nits (batch) |
| `?` | Shortcut help |

### 9.4 Comment Recall

"Recall" button on posted comments. Deletes via GitHub API within configurable window (default: 24h). Comments include hidden `<!-- franktheunicorn-managed -->` marker.

### 9.5 Agent Feedback Panel (AI-Generated PRs)

Assessment (Good / Needs Work / Reject) + free-text + toggle to include findings + toggle to request revision. Posts structured GitHub comment with parseable `<!-- agent-feedback: source -->` header.

---

## 12. Daily Email Digest

Sent at configured time. Plain text + HTML via Django email.

```
Subject: franktheunicorn digest — Mar 27, 2026 — 7 PRs need attention

🔥 HIGH-INTEREST PRs (score ≥ 70)
  apache/spark#54102 — "Add DataFrame.mapInArrow for Connect"
  by huaxin-gao · score: 92 · 3 draft findings ready
  Tests: ✅ differential ✅

📋 YOUR PRs NEEDING ACTION
  apache/spark#53547 — "Python UDF lambda-to-Catalyst transpilation"
  New reviewer comment (2h ago) · Merge conflict (3 files)

🤖 AI-GENERATED PRs
  totallylegitco/fhi#412 — "Add rate limiting to appeal endpoint"
  2 draft findings · Tests: ✅

🚦 MODERATION
  apache/spark#54201 — likely drive-by (no description, no issue)
  totallylegitco/fhi#415 — stale 14 days, consider closing

⚠️ TEST ISSUES
  totallylegitco/fhi#411 — 2 failing on PR branch
  apache/spark#54089 — differential SUSPECT

📊 WEEKLY (Mondays)
  Reviewed: 12 · Posted: 34 · Accuracy: 78% as-is
  Cost: ~$4.20 LLM, 45 min containers
  Anti-patterns: 3 entries suppressed 12 findings this week

💡 MONTHLY (1st)
  3 anti-patterns haven't matched in 60 days — review?
```

---

## 12.5 Alert Mode

### Problem

The digest is a daily summary — fine for routine review, too slow for two
situations: someone raises a PR that collides with work the operator has in
flight (duplicated effort, merge conflicts brewing), and a security report
arrives and sits unnoticed in the queue.

### Design

The worker runs an alert sweep at the end of every poll cycle. Two alert
types:

- **Working overlap** — a PR by someone else in an alert-enabled project
  matches what the operator is working on:
  - it touches the same files as one of the operator's own open PRs in
    that project (automatic — "what I'm working on" ⊇ "my open PRs"), or
  - it touches a declared `working_paths` pattern, or
  - its title/body mentions a declared `working_keywords` term.
- **Security report waiting** — a `SecurityReport` is in the queue
  (status `new`) or in triage (status `triaging`), from any source
  (email ingestion or dashboard paste).

Every alert is recorded as an `Alert` row; a unique `dedup_key` means each
PR / report alerts at most once, so sweeps are idempotent and the first
sweep after enabling produces one batch, not a repeating storm. All alerts
not yet emailed are sent as a **single batched email per cycle** to
`alerts.email` (falling back to `digest_email`), using the same SMTP
config as the digest. No recipient configured → alerts are still recorded
(admin/dashboard) and email is skipped silently.

### Configuration

```yaml
# In config.yaml (operator) — master switch + delivery
alerts:
  enabled: true
  email: "holden@example.com"   # falls back to digest_email
  security_reports: true        # reports with no project honour this

# In project config — participation + what "working on" means
alerts:
  enabled: true                 # default true once operator switch is on
  working_overlap: true
  security_reports: true        # reports attached to this project
  working_paths:
    - "core/src/main/scala/org/apache/spark/storage/"
  working_keywords:
    - "decommission"
```

### Version Targeting

v1. Inert until the operator sets `alerts.enabled` (graceful feature-gate
convention). No LLM calls — pure file/keyword matching against state the
poll cycle already collects.

---

## 13. ET Phone Home (Optional Telemetry)

### What

An optional, enabled-by-default system that pushes anonymized PR review data to a configurable endpoint. The endpoint doesn't exist yet (no infrastructure), but the agent is wired to push when/if the endpoint comes online.

### Why

If the franktheunicorn project grows, aggregate data about how operators use the tool (which features get used, which get ignored, common rejection patterns, calibration data) is valuable for improving defaults and prioritizing development. But this must be:

1. **Crystal clear to users.** No hidden data collection. The init wizard explains it. The config file documents it. The dashboard shows ET status.
2. **Off if the endpoint is down.** The agent checks if the endpoint is reachable before pushing. If it's not (and it won't be initially — there's no server), nothing happens. No errors, no retries, no queue buildup.
3. **Easily disabled.** One config toggle.
4. **Minimal and non-identifying.** No PR content, no code, no comment text. Just aggregate stats.

### Configuration

```yaml
# In config.yaml
telemetry:
  enabled: true  # enabled by default, user clearly informed during init
  endpoint: https://telemetry.franktheunicorn.dev/v1/report  # doesn't exist yet
  # What gets sent (all anonymized, no code/comment content):
  #   - project count, project types (asf/personal/org)
  #   - PRs reviewed per week
  #   - findings approved/edited/rejected counts
  #   - anti-pattern count per project
  #   - feature usage flags (tests enabled, CodeRabbit, fine-tuning, etc.)
  #   - agent version
  #   - error counts by category
  # What NEVER gets sent:
  #   - PR content, diffs, comments, finding text
  #   - repo names, usernames, email addresses
  #   - API keys, tokens
  #   - any personally identifiable information
```

### Init Wizard Language

```
=== Telemetry ===

franktheunicorn can optionally send anonymous usage stats to help improve
the project. This is ON by default but easy to turn off.

What gets sent: aggregate counts (PRs reviewed, findings approved/rejected,
features used). NO code, NO comments, NO usernames, NO repo names.

The telemetry endpoint doesn't exist yet — I haven't built the server.
When/if I do, the agent will start sending. You'll see a notice in the
dashboard when telemetry is actively sending.

[Keep enabled] / [Disable] / [Show me exactly what would be sent]
```

### Implementation

```python
class ETPhoneHome:
    async def maybe_report(self):
        if not self.config.telemetry.enabled:
            return
        
        # Quick check: is the endpoint alive?
        try:
            resp = await self.http.head(
                self.config.telemetry.endpoint,
                timeout=2.0  # don't block the worker
            )
            if resp.status_code != 200:
                return  # endpoint not ready, silently skip
        except (httpx.ConnectError, httpx.TimeoutException):
            return  # endpoint down, silently skip
        
        # Gather anonymous stats
        report = self._build_report()
        
        # Push (fire and forget)
        try:
            await self.http.post(
                self.config.telemetry.endpoint,
                json=report,
                timeout=5.0
            )
        except Exception:
            pass  # never fail the worker due to telemetry
    
    def _build_report(self) -> dict:
        return {
            "agent_version": __version__,
            "project_count": Project.objects.count(),
            "project_types": list(
                Project.objects.values_list("project_type", flat=True)
            ),
            "prs_reviewed_7d": PullRequest.objects.filter(
                updated_at__gte=now() - timedelta(days=7)
            ).count(),
            "findings_approved_7d": ReviewFinding.objects.filter(
                status="approved", created_at__gte=now() - timedelta(days=7)
            ).count(),
            # ... more aggregate stats, never content
        }
```

### Dashboard Indicator

A small indicator in the dashboard footer:
- 📡 **Telemetry: active** (endpoint responding, data being sent)
- 📡 **Telemetry: enabled, endpoint offline** (configured but server not reachable)
- 🔇 **Telemetry: disabled**

---

## 14. Data Model (Django)

```python
class Project(models.Model):
    name = models.CharField(max_length=255, unique=True)
    repo = models.CharField(max_length=255)
    project_type = models.CharField(max_length=20)  # asf, personal, org
    config_yaml = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

class PullRequest(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    github_pr_number = models.IntegerField()
    title = models.CharField(max_length=500)
    author = models.CharField(max_length=255)
    interest_score = models.IntegerField(default=0)
    is_operator_pr = models.BooleanField(default=False)
    has_test_coverage = models.BooleanField(null=True)
    status = models.CharField(max_length=20)  # open, closed, merged
    # Moderation flags
    is_ai_generated = models.BooleanField(default=False)
    ai_agent_source = models.CharField(max_length=100, null=True)
    is_new_contributor = models.BooleanField(default=False)
    is_low_context = models.BooleanField(default=False)
    is_likely_unowned = models.BooleanField(default=False)
    # Queue routing
    queue = models.CharField(max_length=50, default="review")
      # review, ai-generated, new-contributor, consider-closing,
      # needs-triage, your-prs
    # Cached context (v1.5)
    jira_ticket_id = models.CharField(max_length=50, null=True)
    jira_cache = models.JSONField(null=True)
    community_context_cache = models.JSONField(null=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["project", "github_pr_number"]

class ReviewFinding(models.Model):
    """Core abstraction — not a GitHub comment, but a review observation."""
    pr = models.ForeignKey(PullRequest, on_delete=models.CASCADE)
    file_path = models.CharField(max_length=500)
    line_start = models.IntegerField(null=True)
    line_end = models.IntegerField(null=True)
    category = models.CharField(max_length=50)
      # correctness, style, security, test-coverage, architectural,
      # naming, suggested-change, moderation
    severity = models.CharField(max_length=20)
      # critical, important, nit, informational
    body = models.TextField()
    suggestion_diff = models.TextField(null=True)
    reasoning_trace = models.TextField(null=True)
    sources = models.JSONField(default=list)
      # ["agent-primary", "agent-fast", "coderabbit"]
    confidence = models.CharField(max_length=10)
    tone_guard_applied = models.BooleanField(default=False)
    status = models.CharField(max_length=20, default="pending")
      # pending, approved, edited, rejected, posted, recalled
    backend_used = models.CharField(max_length=100)
    operator_edit = models.TextField(null=True)
    rejection_reason = models.TextField(null=True)
    github_comment_id = models.BigIntegerField(null=True)
    posted_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

class TestRun(models.Model):
    pr = models.ForeignKey(PullRequest, on_delete=models.CASCADE)
    run_type = models.CharField(max_length=20)  # pr_branch, base_cherry_pick
    status = models.CharField(max_length=20)
    test_scope = models.JSONField()
    results = models.JSONField(null=True)
    differential_verdict = models.CharField(max_length=20, null=True)
      # good, suspect, broken, infra
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

class AntiPattern(models.Model):
    """Core learning system — visible and editable in dashboard from day one."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    category = models.CharField(max_length=100)
    pattern = models.TextField()
    learned_from = models.TextField()
    is_active = models.BooleanField(default=True)
    times_matched = models.IntegerField(default=0)
    added_at = models.DateTimeField(auto_now_add=True)
    last_matched_at = models.DateTimeField(null=True)

class AgentFeedback(models.Model):
    pr = models.ForeignKey(PullRequest, on_delete=models.CASCADE)
    assessment = models.CharField(max_length=20)
    feedback_body = models.TextField()
    include_findings = models.BooleanField(default=True)
    github_comment_url = models.URLField(null=True)
    sent_at = models.DateTimeField(null=True)

class CostRecord(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    pr = models.ForeignKey(PullRequest, null=True, on_delete=models.SET_NULL)
    action_type = models.CharField(max_length=50)
    backend = models.CharField(max_length=100, null=True)
    tokens_in = models.IntegerField(default=0)
    tokens_out = models.IntegerField(default=0)
    estimated_cost_usd = models.DecimalField(max_digits=8, decimal_places=4)
    duration_seconds = models.FloatField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
```

---

## 15. Self-Hosting & Onboarding

### 12.1 Personas

| Setting | Solo Maintainer | Team Lead |
|---------|----------------|-----------|
| Interest threshold | 30 | 50 |
| Worker concurrency | 2 | 4 |
| Email digest | Daily | Daily + weekly |

Both profiles ship as example YAML. `review-agent init` lets you pick.

### 12.2 Five-Minute Start

```bash
git clone https://github.com/holdenk/franktheunicorn.git
cd franktheunicorn
cp .env.example .env
# Edit .env: GITHUB_TOKEN + ANTHROPIC_API_KEY (minimum)

docker compose up
# Dashboard: http://localhost:7742
# Worker starts polling
```

`docker-compose.yaml`: `web` (Django) + `worker`. Optional `postgres` (commented out).

**Graceful feature gates:** Missing config → feature skipped, not error. No SMTP → no email. No container runtime → no tests. Dashboard always works.

### 12.3 Env Vars

```bash
# Required:
GITHUB_TOKEN=ghp_...
ANTHROPIC_API_KEY=sk-ant-...

# Optional:
GITHUB_TOKEN_BOT=ghp_...              # v1.5: auto-posting
CODERABBIT_API_KEY=...                # v1.5: CodeRabbit CLI
REVIEW_AGENT_SMTP_HOST=...            # email digest
REVIEW_AGENT_SMTP_PORT=587
REVIEW_AGENT_SMTP_USER=...
REVIEW_AGENT_SMTP_PASS=...
REVIEW_AGENT_EMAIL_FROM=...
DATABASE_URL=postgresql://...         # default: SQLite
```

### 12.4 State & Backup

All state lives in `~/.review-agent/`:

```
~/.review-agent/
  config.yaml                 # operator profile
  db.sqlite3                  # all Django models
  projects/
    spark.yaml
    spark.anti-patterns.yaml
    fhi.yaml
    fhi.anti-patterns.yaml
  voice/
    spark/voice_curated.jsonl
    fhi/voice_curated.jsonl
  training-data/              # v2: for fine-tuning
    spark/actions.jsonl
  cache/                      # v1.5: downstream API tracking
```

Backup: `tar czf franktheunicorn-backup.tar.gz ~/.review-agent/`. Restore: untar. No database migrations, no state sync, no cloud dependency.

### 12.5 CLI Reference

```bash
review-agent init                           # interactive setup
review-agent add-project --repo org/repo    # generate project YAML
review-agent serve                          # Django dev server / gunicorn
review-agent worker start                   # background worker (or run via cron)
review-agent worker status                  # queue depth, active reviews
review-agent curate-voice --project spark   # Comment Curator TUI
# review-agent triage                       # CLI triage (deferred to v3)
review-agent fine-tune --project spark      # Axolotl (v2)
```

---

## 16. Implementation Phases

### Phase 1 (v1): Foundation + Core Review (4-5 weeks)

**Week 1-2: Plumbing**
- Django scaffold, pytest + coverage.py (90% from day one)
- Dual-path data access layer: GitHub PRs, diffs, reviews, blame, user info
  - API path + scrape fallback for each
  - Contract tests for output equality
  - `pyrate-limiter` with SQLite buckets + adaptive GitHub headers
  - `review-agent record-fixtures` for test fixture generation
- Project config schema + YAML loader
- PR ingestion + interest scoring (all signals including blame layers 1-2)
- Auto-detect collaborators CLI
- Committer-is-on-it detection + deranking
- Moderation flag detection + queue routing
- All Django models
- Docker Compose packaging + graceful feature gates

**Week 3-4: Dashboard + Review Pipeline**
- Dashboard: inbox, your-PRs, queue tabs, workspace selector, responsive/mobile
- Keyboard shortcuts
- Anti-pattern manager (full CRUD, prominent in UI)
- Multi-backend LLM review with context injection
- Tone Guard rewriting pass
- Review Finding abstraction + backend dedup
- Suggested changes generation
- Keyboard-driven approve/edit/reject
- Batch posting as single GitHub review
- Attribution footer
- Tier 1 learning: anti-pattern auto-suggestion + edit history pairs

**Week 5: Email + Polish**
- Daily email digest (plain text + HTML)
- Workspace-aware digest (per-workspace schedules)
- Cost tracking
- Documentation: README, quick start, config reference
- `review-agent init` with persona profiles

### Phase 2 (v1 continued): Test Verification (2 weeks)
- Container-based differential testing (Docker)
- Three-source test identification
- Import-failure detection
- Dashboard + email integration
- No-coverage badge + draft finding

### Phase 3 (v1.25): Agent Feedback Channel (1 week)
- Session link detection from PR descriptions
- "Send Feedback to Session" button (Claude Code URL-open, Codex API)
- Fallback to GitHub comment

### Phase 4 (v1.5): Context + Social (2-3 weeks)
- Community context search (mailing lists, Discord, Discourse)
- JIRA lazy-fetch + cache
- CodeRabbit CLI + finding-level dedup
- Cross-project downstream impact
- Confidence-gated auto-posting (triple gate: confidence + anti-pattern + tone guard)
- Comment Curator CLI (Textual TUI)
- Perplexity API integration (general + technical search modes)
- GitHub Issues context (linked + related issue search)
- Sentry integration (error context + scoring signal)
- CLI triage mode (deferred to v3)

### Phase 5 (v1.75): Bayesian Learning (1-2 weeks)
- Rejection predictor (sklearn Naive Bayes / logistic regression)
- Auto-suppress high-P(rejection) findings
- Dashboard: "suppressed" section, rejection probability display
- Auto-retrain on 50-action intervals

### Phase 6 (v2): Personal Model + Hub (3-4 weeks)
- Axolotl training data export pipeline
- Generated Axolotl config (QLoRA, Qwen2.5-Coder-7B default)
- Training execution (local GPU, Docker, or remote)
- Evaluation metrics + pass/fail gates
- Ollama serving integration
- Shepherding mode (draft responses to reviewer comments on operator's PRs)
- Merge queue + auto-merge
- K8s Helm chart

---

## 17. Testing Strategy

### 14.1 Layers

| Layer | Scope | Tools |
|-------|-------|-------|
| Unit | Config, scoring, gating, anti-patterns, tone guard, finding dedup | pytest |
| Integration | GitHub API, LLM backends, Docker test runner | responses, VCR.py |
| Model | ORM, migrations, data integrity | pytest-django |
| View | Dashboard, keyboard shortcuts, mobile, anti-pattern manager | Django test client |
| E2E | Full pipeline: ingestion → findings → posting | Docker-in-Docker, fixtures |

### 14.2 Tooling

pytest, coverage.py (90% in CI), pytest-django, responses/httpx-mock, VCR.py, factory_boy, ruff, mypy.

### 14.3 CI

```yaml
- pytest --cov --cov-fail-under=90
- ruff check .
- mypy .
- python manage.py check --deploy
```

---

## 17.5 Dual-Path Testing (API + Scrape)

### Dual-Path Testing

The biggest testing addition is the dual-path data access layer. Every data source has:

1. **API path tests** — VCR cassettes for HTTP recording/replay
2. **Scrape path tests** — saved HTML fixtures
3. **Contract tests** — both paths produce identical output for same input
4. **Rate limiter tests** — verify limiter integration, fallback behavior
5. **Integration tests** — unified `fetch()` with mock API failures triggering scrape fallback

```
tests/
  data_access/
    conftest.py                   # shared fixtures, fetcher factories
    test_dual_path_contract.py    # parametrized: same test, both paths
    github/
      test_prs_api.py
      test_prs_scrape.py
      test_reviews_api.py
      test_reviews_scrape.py
      test_blame_api.py
      test_blame_scrape.py
      test_user_api.py
      test_user_scrape.py
      test_github_rate_limiter.py
      fixtures/
        pr_list_response.json
        pr_list_page.html
        pr_diff_response.diff
        pr_diff_page.html
        blame_graphql_response.json
        blame_page.html
        reviews_response.json
        reviews_page.html
    jira/
      test_tickets_api.py
      test_tickets_scrape.py
      fixtures/
        ticket_response.json
        ticket_page.html
    mailing_list/
      test_archive_search_api.py
      test_archive_scrape.py
      fixtures/
        search_results.json
        archive_page.html
  scoring/
    test_interest_scoring.py
    test_blame_watching.py
    test_collaborator_detection.py
    test_moderation_flags.py
  review/
    test_finding_generation.py
    test_tone_guard.py
    test_anti_pattern_matching.py
    test_dedup.py
    test_suggested_changes.py
  voice/
    test_comment_curator.py
    test_voice_loading.py
  fine_tuning/                    # v2
    test_data_export.py
    test_axolotl_config_gen.py
    test_eval_metrics.py
  worker/
    test_priority_queue.py
    test_resumability.py
    test_concurrency.py
  dashboard/
    test_inbox_view.py
    test_pr_detail_view.py
    test_anti_pattern_manager.py
    test_keyboard_shortcuts.py
    test_mobile_responsive.py
```

### Coverage Target

90%+ overall, with 95%+ on the data access layer (this is where bugs hide — API format changes, HTML structure changes, rate limit edge cases).

---

## 18. State Directory

```
~/.review-agent/
  config.yaml                          # operator profile
  db.sqlite3                           # all Django models
  rate-limits.sqlite                   # pyrate-limiter state (persists across restarts)
  projects/
    spark.yaml                         # project config
    spark.anti-patterns.yaml           # learned anti-patterns (core feedback system)
    spark.collaborators.yaml           # auto-detected + manual collaborators
    fhi.yaml
    fhi.anti-patterns.yaml
    fhi.collaborators.yaml
  voice/
    spark/voice_curated.jsonl          # curated voice dataset
    fhi/voice_curated.jsonl
  training-data/                       # v2: Axolotl fine-tuning
    spark/
      train.jsonl
      eval.jsonl
      metadata.json
      axolotl_config.yaml
  models/                              # v2: fine-tuned model adapters
    spark/
      v1/
        adapter_model.safetensors
        adapter_config.json
        training_metadata.json
        eval_results.json
  cache/
    blame/                             # git blame cache (keyed by file+commit)
    jira/                              # JIRA ticket cache (v1.5)
    community/                         # mailing list search cache (v1.5)
    downstream/                        # tracked API imports (v1.5)
      spark-testing-base-imports.json
      snowflake-connector-imports.json
```

Backup: `tar czf franktheunicorn-backup.tar.gz ~/.review-agent/`

---

## 19. CLI Reference (Complete)

```bash
# --- Setup ---
review-agent init                              # interactive setup
review-agent add-project --repo org/repo       # generate project YAML

# --- Run ---
review-agent serve                             # Django dashboard
review-agent worker start                      # background worker (daemon)
review-agent worker status                     # queue depth, rate limit status

# --- Collaborators ---
review-agent detect-collaborators --project spark      # analyze git + reviews
review-agent detect-collaborators --project spark --dry-run
review-agent detect-collaborators --all                # all projects

# --- Voice ---
review-agent curate-voice --project spark              # Comment Curator TUI

# --- Custom Scoring ---
review-agent regenerate-scoring --project spark        # LLM-generate scoring fn
review-agent regenerate-scoring --project spark --preserve-edits
review-agent test-scoring --project spark --pr 54102   # test against a PR

# --- Data Access ---
review-agent record-fixtures --source github --project spark  # record test fixtures
review-agent check-rate-limits                         # show rate limit status

# --- Rejection Predictor (v1.75) ---
review-agent train-rejection-model --project spark

# --- Fine-Tuning (v2) ---
review-agent export-training-data --project spark
review-agent refresh-dataset --project spark           # append new actions to JSONL
review-agent dataset-stats --project spark              # dataset composition + size
review-agent fine-tune --project spark [--base-model ...]
review-agent fine-tune --project spark --eval-only

# --- Triage (deferred to v3) ---
# review-agent triage                                  # CLI triage mode (v3)
```

---

## 20. Resolved Design Decisions

| Question | Resolution |
|----------|-----------|
| Committer list source | ASF: project committers page. Others: GitHub org members. Weekly refresh. |
| Workspace switching | Manual toggle only. No auto-detection. |
| Claude Code feedback API | To research. Worst case: Selenium. Not a blocker. |
| Blame approach | Run fresh each time on changed files. No cache. Simple. |
| Rejection model cold start | User runs setup script when ready. No auto-training. |
| Low-context drive-by learning | Learn from closed-without-merge PRs with context-requesting comments. |
| Tone Guard calibration | Prompt for examples during init. Solid defaults with 3 calibration examples. |
| Worker scheduling | Daemon mode default. Cron also works (idempotent). |
| Finding → GitHub mapping | Multi-line supported via `start_line` + `line` API params. |
| Fine-tuning VRAM | Train offline. Turn off serving model. No contention. |
| Container security | All test execution in rootless Docker. No network. Resource-capped. Optional Firecracker. |
| Container images | Per-project configurable. Auto-build from requirements if not specified. |
| Telemetry | ET phone home enabled by default, clearly disclosed, endpoint doesn't exist yet. |

---

## 21. Research Items

1. **Claude Code feedback API**: Does Claude Code expose an API for receiving feedback programmatically? Worst case: Selenium-style browser automation. Not a blocker for v1.25.

2. **Moderation flag tuning**: Thresholds for `low_context_driveby` need empirical calibration per-project. Learn from closed-without-merge PRs.

3. **Comment Curator TUI framework**: Textual is the likely choice for the interactive curation interface.

4. **Scrape stability monitoring**: Weekly CI job to verify API/scrape output parity. Alert on GitHub HTML structure changes.

5. **Axolotl base model quality**: Empirical validation needed for minimum dataset size (likely 200+) and quality thresholds for Qwen2.5-Coder-7B.

6. **Firecracker integration**: For operators wanting microVM isolation — needs investigation of rootfs preparation and kernel management.