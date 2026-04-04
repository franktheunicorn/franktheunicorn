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
cp .env.example .env          # edit to set FRANK_GITHUB_TOKEN + API keys
```

### 2. Set up your config

Copy the example config and edit it for your repos:

```bash
cp config/examples/operator.yaml config/active/operator.yaml
cp -r config/examples/projects/ config/active/projects/
# Edit config/active/operator.yaml — set github_username, llm_backends, etc.
# Edit/add project files in config/active/projects/
```

All config lives in `config/active/` (gitignored). Examples in `config/examples/`.

### 3. Run it

**Guided setup** (recommended for first time):
```bash
./scripts/setup.sh
```

**Docker Compose** (no Python needed):
```bash
docker compose up              # dashboard: http://localhost:8000
```

**Make** (local development):
```bash
make setup                     # creates venv, installs deps, runs migrations
make serve                     # dashboard: http://localhost:8000
make worker                    # background PR poller (separate terminal)
```

Both paths default to **mock mode** — fixture data, no API keys needed.
Set `FRANK_MOCK_MODE=false` in `.env` with real tokens for live PR ingestion.

For project configuration and workspaces, see the **[Install Guide](docs/install.md)**.

## What It Does

- **Triages PRs** across multiple repos with configurable interest scoring (path overlap, git blame, collaborator detection, custom LLM-generated scoring functions)
- **Drafts review comments** using multi-backend LLM review (Sonnet for substance, Haiku for nits, Opus for architecture)
- **Learns from your corrections** via an anti-pattern list that improves with every rejection and edit
- **Runs differential tests** to verify new tests actually fail without the PR's changes
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
├── github/       # httpx client + mock client with fixture data
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
