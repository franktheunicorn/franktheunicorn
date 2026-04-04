# Install Guide

This guide walks you through getting franktheunicorn running and configured.
For the impatient, the [README](../README.md) has a quick start that gets you a
dashboard in under a minute using mock data.

**Prefer a guided experience?** Run `./scripts/setup.sh` — it walks you
through prerequisites, mode selection (Docker or local), API key configuration,
and project initialization interactively. The steps below are the same things
the script does, documented for reference.

## Prerequisites

- **Git**
- **Python 3.11+** (for local/make setup) or **Docker** (for container setup)
- **GitHub personal access token** — for real PR ingestion (not needed in mock mode)
- **LLM API key** — Anthropic, OpenAI, Google, or a local Ollama install (not needed in mock mode)

## 1. Clone and configure environment

```bash
git clone https://github.com/franktheunicorn/franktheunicorn.git
cd franktheunicorn
cp .env.example .env
```

Edit `.env`. For a quick demo with fixture data, the defaults work as-is
(`FRANK_MOCK_MODE=true`). For real PR ingestion, set:

```bash
FRANK_MOCK_MODE=false
FRANK_GITHUB_TOKEN=ghp_your_token_here
ANTHROPIC_API_KEY=sk-ant-your-key-here   # or OPENAI_API_KEY / GOOGLE_API_KEY
```

## 2. Start the services

Pick **one** of the two paths below.

### Option A: Docker Compose

No Python install needed. Docker handles everything.

```bash
docker compose up
```

This starts two containers:
- **web** — Django dashboard at <http://localhost:8000> (runs migrations automatically)
- **worker** — background poller that ingests PRs

State is persisted in `./data/` (SQLite) and config is read from `./config/`.

To stop: `Ctrl-C` or `docker compose down`. To rebuild after code changes:
`docker compose build && docker compose up`.

### Option B: Make (local development)

```bash
make setup    # creates venv, installs deps, runs migrations
```

Then start both services (in separate terminals, or use a multiplexer):

```bash
make serve    # terminal 1 — dashboard at http://localhost:8000
make worker   # terminal 2 — background poller
```

`make setup` will warn you if `.env` doesn't exist yet.

Other useful targets:

| Command          | What it does                        |
|------------------|-------------------------------------|
| `make check`     | Run lint + typecheck + tests        |
| `make test`      | Run tests with coverage             |
| `make lint`      | Check linting and formatting        |
| `make format`    | Auto-format code                    |
| `make typecheck` | Run mypy                            |
| `make clean`     | Remove caches and build artifacts   |

## 3. Verify it works

Open <http://localhost:8000>. In mock mode you'll see fixture PRs immediately.
In real mode, the worker needs a polling cycle (default: 5 minutes) to ingest
PRs — or restart the worker to trigger an immediate poll.

## 4. Configure your projects

Out of the box, franktheunicorn uses the example configs in `config/examples/`.
To monitor your own repos, create your own operator and project configs.

### Initialize operator config

```bash
python manage.py init_project
```

This creates `config/active/operator.yaml` with your GitHub username and review
style. (If using Docker, run commands with `docker compose exec web`.)

### Add projects

Use the management command for each repo you want to monitor:

```bash
# Personal project — light review, friendly tone
python manage.py add_project \
  --repo franktheunicorn/franktheunicorn \
  --governance personal \
  --tone "friendly and brief"

# Work project — ASF governance, thorough review
python manage.py add_project \
  --repo apache/spark \
  --governance asf \
  --tone "constructive and thorough"
```

This creates YAML files in `config/active/projects/`. You can also copy and
edit the examples directly:

```bash
cp config/examples/projects/personal-django.yaml config/active/projects/franktheunicorn-franktheunicorn.yaml
cp config/examples/projects/apache-spark.yaml config/active/projects/apache-spark.yaml
# Edit each file to match your repos
```

### Example: franktheunicorn project config

```yaml
# config/active/projects/franktheunicorn-franktheunicorn.yaml
owner: "franktheunicorn"
repo: "franktheunicorn"
review_context: "personal review assistant — pragmatic, ship it"
governance: "personal"
watched_paths:
  - "src/"
  - "tests/"
ignore_paths:
  - "docs/"
tone: "friendly and brief"
test_expectations: "tests nice to have but not blocking"
enabled: true
```

### Example: Apache Spark project config

```yaml
# config/active/projects/apache-spark.yaml
owner: "apache"
repo: "spark"
review_context: "ASF governance — formal review required for all changes"
governance: "asf"
watched_paths:
  - "sql/catalyst/"
  - "python/pyspark/"
  - "core/src/main/"
ignore_paths:
  - "docs/"
  - "resource-managers/"
tone: "constructive and thorough"
test_expectations: "tests required for all new features and bug fixes"
frequent_contributors:
  - "cloud-fan"
  - "dongjoon-hyun"
  - "HyukjinKwon"
enabled: true
```

## 5. Configure workspaces

Workspaces let you filter the dashboard by context — e.g., "work" vs "personal".
Add a `workspaces` section to your operator config:

```yaml
# config/active/operator.yaml
github_username: "yourname"
review_style: "direct but kind"
auto_post: false
poll_interval_seconds: 300

workspaces:
  personal:
    projects: ["franktheunicorn/franktheunicorn"]
    description: "Side projects"
  work:
    projects: ["apache/spark"]
    description: "ASF work"
  all:
    projects: "*"
    description: "Everything"
```

In the dashboard, use the workspace dropdown (top of page) to switch context.
Your selection is saved in a cookie and persists across sessions.

## 6. Configure LLM backends

The interactive wizard walks you through provider selection:

```bash
python manage.py setup_llm
```

It supports Claude, OpenAI, Gemini, and local models via Ollama. You can
enable multiple backends simultaneously — findings are combined and deduped.

Or edit the operator config directly:

```yaml
# config/active/operator.yaml (add to existing config)
llm_backends:
  - provider: "claude"
    model: "claude-sonnet-4-20250514"
    api_key_env: "ANTHROPIC_API_KEY"
    temperature: 0.3
    max_tokens: 4096
```

For a free, local option (no API key needed):

```bash
# Install Ollama: https://ollama.com/download
ollama pull qwen2.5-coder:14b
```

```yaml
llm_backends:
  - provider: "ollama"
    model: "qwen2.5-coder:14b"
    base_url: "http://localhost:11434"
```

## 7. Config location

All user config lives in `config/active/` (gitignored). The app auto-detects it:
if `config/active/operator.yaml` exists, it's used; otherwise `config/examples/`
is used as a fallback.

You can override with environment variables in `.env`:

```bash
FRANK_OPERATOR_CONFIG=config/active/operator.yaml
FRANK_PROJECTS_DIR=config/active/projects
```

Restart services after config changes.

## Mock mode vs real mode

| | Mock mode (`FRANK_MOCK_MODE=true`) | Real mode (`FRANK_MOCK_MODE=false`) |
|---|---|---|
| **Tokens needed** | None | `FRANK_GITHUB_TOKEN` + LLM API key |
| **Data source** | Fixture JSON in `config/fixtures/` | Live GitHub API |
| **Good for** | Demo, development, testing | Actual PR review |
| **Default** | Yes | No |

Mock mode is the default so you can try the dashboard immediately without any
API keys.

## Troubleshooting

**Port 8000 already in use** — Another service is using it. Either stop that
service or change the port: `python manage.py runserver 8001` (or edit
`compose.yaml` ports).

**Worker not picking up PRs** — Check that `FRANK_MOCK_MODE=false` and
`FRANK_GITHUB_TOKEN` is set. The worker polls every `FRANK_POLL_INTERVAL`
seconds (default 300). Restart the worker to trigger an immediate poll.

**`make setup` fails on missing Python** — You need Python 3.11+. Check with
`python3 --version`. On macOS: `brew install python@3.12`. On Ubuntu:
`sudo apt install python3.12 python3.12-venv`.

**Docker build fails** — Make sure Docker is running. Try `docker compose build
--no-cache` for a clean build.
