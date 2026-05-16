** THIS IS NOT READY YET **

# 🦄 franktheunicorn

A local-first AI code review assistant for open-source maintainers.

franktheunicorn monitors PRs across your projects, triages them by relevance, drafts review comments in your voice, and learns from your corrections. You clone it, configure it for your repos, and run it on your own machine.

Want to play with it? Clone it, configure it for your repos, run it on your own machine. State lives in SQLite and local files.


This is (roughly) my 3rd attempt at building something like this (now that LLMs work it's a whole new world): of a line of review tooling: [holdensmagicalunicorn](https://github.com/holdenk/holdensmagicalunicorn) (2012) → [predict-pr-comments](https://github.com/franktheunicorn/predict-pr-comments) (2018, [FOSDEM talk](https://archive.fosdem.org/2019/schedule/event/ml_on_code_code_review_mailing_list/)) → this.

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/franktheunicorn/franktheunicorn.git
cd franktheunicorn
```

### 2. Set up your config

All configuration lives in `config/active/operator.yaml` (the single source of truth).
Secrets (API keys) go in `.env` and are referenced via `${VAR}` syntax in YAML.

```bash
# Config
cp config/examples/operator.yaml config/active/operator.yaml
cp -r config/examples/projects/ config/active/projects/
# Edit config/active/operator.yaml — set github_username, llm_backends, mock_mode, etc.

# Secrets
cp .env.example .env           # add API keys here
```

### 3. Run it

**Guided setup** (recommended for first time):
```bash
./scripts/setup.sh
```

**Docker Compose** (no Python needed):
```bash
docker compose up              # dashboard: http://localhost:7742
```

**Make** (local development):
```bash
make setup                     # creates venv, installs deps, runs migrations
make serve                     # dashboard: http://localhost:8000
make worker                    # background PR poller (separate terminal)
```

To try it without API keys, set `mock_mode: true` in `config/active/operator.yaml` — this uses fixture data so you can explore the dashboard immediately.
Set `mock_mode: false` (the default) with real tokens in `.env` for live PR ingestion.

For project configuration and workspaces, see the **[Install Guide](docs/install.md)**.

## What It Does

- **Triages PRs** across multiple repos and forges (GitHub, Gitea, Forgejo / Codeberg, GitLab) with configurable interest scoring (path overlap, git blame, collaborator detection, custom LLM-generated scoring functions)
- **Drafts review comments** using multi-backend LLM review (Sonnet for substance, Haiku for nits, Opus for architecture)
- **Learns from your corrections** via an anti-pattern list that improves with every rejection and edit
- **Runs differential tests** to verify new tests actually fail without the PR's changes — see [Dual Run Tests](#dual-run-tests) below and [docs/test-runner.md](docs/test-runner.md) to enable per project
- **Routes PRs to queues** — AI-generated, new contributors, low-context drive-bys, consider-closing
- **Respects project cultures** — ASF governance norms are different from your Django side project
- **Works on your phone** — responsive dashboard over Tailscale for conference triage

## What It Doesn't Do

- Post comments without your approval (draft-only in v1)
- Require a cloud account, SaaS subscription, or uptime
- Replace your judgment — it assists, you decide

## Core Principles

1. **Local-first.** SQLite + files. Backup with `tar`. No cloud dependency.
2. **Clone and run.** Five-minute start. Every feature beyond PR ingestion is opt-in.
3. **Operator-in-the-loop.** The agent drafts; you post.
4. **Project-aware.** Each repo has its own review norms, tone, and expectations.
5. **Learns from corrections.** Anti-patterns → rejection predictor → fine-tuned model (three tiers, each optional).

## Dual Run Tests

franktheunicorn can run a PR's scoped tests twice — once on the PR branch and
once on the base branch with the new test files cherry-picked on top — to
check whether the new tests actually validate the change. This catches
tautological tests that pass everywhere and regressions in the same sweep.

### What the verdicts mean

| Badge | Meaning |
|-------|---------|
| **GOOD** (green) | New tests fail on base, pass on PR. The tests validate the change. |
| **SUSPECT** (yellow) | Tests pass on both branches — likely tautological, not actually testing the change. |
| **BROKEN** (red) | Tests fail on the PR branch — possible regression introduced. |
| **INFRA** (blue) | Base run hit an import/setup error — result is inconclusive. |

Verdicts appear on the home page PR list and on each PR detail page.
A **Run Dual Tests** button on the PR detail page lets you trigger a run
manually for any PR, even if the automatic run was skipped.

### Prerequisites

The worker needs a Docker daemon (rootless Docker recommended). The web
container never gets Docker access — only the worker spawns test containers.

### Enabling for a project

Add a `tests:` block to your project YAML under `config/active/projects/`.

**Apache Spark example** (upstream project with a CI image):

```yaml
# config/active/projects/apache-spark.yaml
owner: apache
repo: spark
committers:
  - holdenk         # replace with your GitHub username — operator gets trusted-author runs
  - mridulm
  - cloud-fan
tests:
  enabled: true
  container_image: ghcr.io/apache/spark-test:latest
  resource_tier: heavy          # 8 CPU, 16 GB RAM, 45 min timeout
  test_command: "python -m pytest {tests} --tb=short -q"
  workdir: /workspace
```

**Personal project with a repo Dockerfile:**

```yaml
# config/active/projects/my-app.yaml
owner: holdenk
repo: my-app
tests:
  enabled: true
  dockerfile: .frank/Dockerfile  # path inside the repo
  resource_tier: standard        # 4 CPU, 8 GB, 15 min (default)
  test_command: "pytest {tests} --tb=short -q"
  workdir: /app
```

**Zero-Dockerfile auto-build:**

```yaml
# config/active/projects/example.yaml
owner: example
repo: hello
tests:
  enabled: true
  resource_tier: light           # 2 CPU, 4 GB, 5 min
  auto_build:
    base_image: python:3.12-slim
    requirements_files:
      - requirements.txt
      - requirements-test.txt
    setup_commands:
      - pip install -e .
```

### Trusted-author gate

Automatic differential runs (triggered by the worker) only fire for
**trusted authors** — committers and frequent contributors listed in your
project YAML. This prevents running arbitrary code from unknown contributors
without your review.

The **Run Dual Tests** button on the PR detail page bypasses this gate,
letting you manually run tests on any PR after you have reviewed the code.

See [docs/test-runner.md](docs/test-runner.md) for the full reference.

## Documentation

- **[Install Guide](docs/install.md)** — full setup, project configuration, and workspace setup
- **[Master Design Document](docs/franktheunicorn-master-design.md)** — the full architecture, all decisions, implementation phases
- **[Security Design](docs/security-design.md)** — threat model, trust boundaries, attack surfaces, CI trust rules, and hardening checklist
- **[CLAUDE.md](CLAUDE.md)** — instructions for Claude Code working on this repo
- **[AGENTS.md](AGENTS.md)** — instructions for any AI agent contributing
- **[REVIEWER.md](REVIEWER.md)** — instructions for franktheunicorn reviewing its own PRs

## Code layout

```
src/franktheunicorn/
├── config/       # Pydantic models + YAML loader
├── core/         # Django models (Project, PR, ReviewDraft, AntiPattern, OperatorAction)
├── backends/     # Forge-agnostic ABC + GitHub / Gitea-Forgejo / GitLab clients
├── scoring/      # Interest scoring (author, review request, paths, contributors, bots)
├── review/       # Stub LLM drafter + anti-pattern detection
├── digest/       # Daily digest stub
├── dashboard/    # Django views + server-rendered HTML templates
└── worker/       # Polling loop
```



## Version Roadmap

| Version | What | Status |
|---------|------|--------|
| **v1** | Triage + draft reviews + anti-pattern learning + dashboard + digest | 🚧 Building |
| v1.25 | Direct feedback to Claude Code / Codex sessions | Designed |
| v1.5 | Community context, JIRA, CodeRabbit, auto-posting | Designed |
| v1.75 | Rejection predictor (sklearn) | Designed |
| v2 | Fine-tuned personal model (Axolotl), merge queue, shepherding | Designed |

## Project Structure

```
src/franktheunicorn/
  core/           # Models, config, shared types
  scoring/        # Interest scoring, blame, collaborators, custom scoring
  review/         # LLM pipeline, findings, tone guard, anti-patterns
  data_access/    # Dual-path fetchers (API + scrape) for GitHub, JIRA, etc.
  worker/         # Background polling + review pipeline
  dashboard/      # Django views + htmx templates
  digest/         # Email digest
  config/         # YAML schema + loaders
tests/            # Parallel structure, 90%+ coverage enforced
```

## License

Apache 2.0

## Contributing

PRs welcome. Read [AGENTS.md](AGENTS.md) if you're an AI agent, or just follow the patterns in the existing code. All PRs need tests. All data access needs dual-path (API + scrape). Dashboard must work on mobile.

## Similar Tools & Related Projects

franktheunicorn is local-first, operator-in-the-loop, and learns from your corrections. If that's not what you need, there are great alternatives:

### AI Code Review

- [CodeRabbit](https://coderabbit.ai/) — AI code review bot that posts inline comments on PRs
- [Codium PR-Agent](https://github.com/Codium-ai/pr-agent) — open-source AI PR reviewer (also available as hosted service)
- [Greptile](https://greptile.com/) — AI code review with codebase context
- [Sourcery](https://sourcery.ai/) — AI code reviewer focused on Python
- [Amazon CodeGuru Reviewer](https://aws.amazon.com/codeguru/) — AWS ML-powered code review

### PR Automation & Review Routing

- [Danger](https://danger.systems/) — rule-based PR automation (Ruby/JS/Swift/etc.)
- [PullApprove](https://www.pullapprove.com/) — code review assignment and policy
- [CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners) — GitHub's built-in review routing

### PR Triage & Dashboards

- [Graphite](https://graphite.dev/) — stacked PRs and review dashboard
- [spark-pr-dashboard](https://github.com/databricks/spark-pr-dashboard) — Databricks' PR triage dashboard for Apache Spark (inspired some of our down-ranking logic)

### AI Coding Agents

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — Anthropic's CLI coding agent
- [OpenAI Codex](https://openai.com/index/openai-codex/) — OpenAI's coding agent
- [Aider](https://github.com/paul-gauthier/aider) — open-source AI pair programming in the terminal
