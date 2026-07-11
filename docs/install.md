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

### macOS setup

On a fresh Mac (including MacBook Air with Apple Silicon), install these first:

```bash
# Xcode Command Line Tools (provides git, clang, and system headers)
xcode-select --install

# Homebrew (package manager) — see https://brew.sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.12 (macOS ships with a stub that prompts for Xcode CLT, not a full Python)
brew install python@3.12

# Docker Desktop (if using the Docker setup path)
brew install --cask docker
```

Apple Silicon (M1/M2/M3) Macs are fully supported — all Python dependencies
install natively and Docker runs via Rosetta or native ARM images.

## 1. Clone and configure

```bash
git clone https://github.com/franktheunicorn/franktheunicorn.git
cd franktheunicorn

# Config (single source of truth)
cp config/examples/operator.yaml config/active/operator.yaml

# Secrets (API keys, tokens — referenced via ${VAR} in operator.yaml)
cp .env.example .env
```

Edit `config/active/operator.yaml`. For a quick demo with fixture data, the
defaults work as-is (`mock_mode: false` but no token needed in mock mode).
For real PR ingestion, set `mock_mode: false` in `operator.yaml` and add
your secrets to `.env`:

```bash
# .env (secrets only)
FRANK_GITHUB_TOKEN=ghp_your_token_here
ANTHROPIC_API_KEY=sk-ant-your-key-here   # or OPENAI_API_KEY / GOOGLE_API_KEY
```

## 2. Start the services

Pick **one** of the paths below.

### Option A: Docker Compose

No Python install needed. Docker handles everything.

```bash
docker compose up
```

This starts two containers:
- **web** — Django dashboard at <http://localhost:7742> (runs migrations automatically)
- **worker** — background poller that ingests PRs

State is persisted in `./data/` (SQLite) and config is read from `./config/`.

To stop: `Ctrl-C` or `docker compose down`. To rebuild after code changes:
`docker compose build && docker compose up`.

### Option B: Non-Docker single-command launch

Use this when you want the same two app processes as Docker Compose, but
running directly on your machine.

```bash
make setup    # first time only: creates venv, installs deps, runs migrations
make up       # dashboard + worker at http://localhost:7742
```

`make up` runs `scripts/run_local_all.sh`. It runs migrations and
`collectstatic`, starts gunicorn on <http://localhost:7742>, waits for the
dashboard to respond, then starts the worker. The script uses `screen` if
available, then `tmux`, then `nohup`, with logs in `data/logs/`.

Useful commands:

```bash
make up-status                 # process state + attach commands
./scripts/run_local_all.sh logs # follow web + worker logs
make down                      # stop both processes
```

Both Docker Compose and this non-Docker launcher are supported.

### Option C: Make (manual local development)

```bash
make setup    # creates venv, installs deps, runs migrations
```

Then start both services (in separate terminals, or use a multiplexer):

```bash
make serve    # terminal 1 — dashboard at http://localhost:8000
make worker   # terminal 2 — background poller
```

`make setup` creates the virtualenv and installs dependencies.

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

Open <http://localhost:7742>. In mock mode you'll see fixture PRs immediately.
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

## 4b. Other forges (Forgejo, Gitea, GitLab)

franktheunicorn talks to GitHub by default, but the same poll/draft/post
pipeline works against any of these forges. Each project YAML names the
forge it belongs to via `forge:`; operator-level `forges:` registers
each instance with its base URL and token.

### Mint a personal access token

- **Forgejo / Codeberg:** Settings → Applications → Manage Access Tokens
  → "Generate New Token". Scopes: `read:repository`, `write:issue`,
  `write:repository`.
- **Gitea (self-hosted):** Settings → Applications → "Generate New
  Token". Same scopes as Forgejo.
- **GitLab / GitLab.com:** User Settings → Access Tokens. Scopes:
  `api` (or the narrower combo `read_api` + `write_repository`).

Put the token in `.env`:

```bash
# .env
FRANK_CODEBERG_TOKEN=cb_pat_...
FRANK_GITEA_TOKEN=...
FRANK_GITLAB_TOKEN=glpat-...
```

### Register the forge in operator.yaml

```yaml
# config/active/operator.yaml
forges:
  - name: github
    type: github
    token: ${FRANK_GITHUB_TOKEN}
  - name: codeberg
    type: forgejo
    base_url: https://codeberg.org
    token: ${FRANK_CODEBERG_TOKEN}
  - name: work-gitea
    type: gitea
    base_url: https://git.work.example
    token: ${FRANK_GITEA_TOKEN}
  - name: gitlab
    type: gitlab
    base_url: https://gitlab.com
    token: ${FRANK_GITLAB_TOKEN}
```

If you only use github.com you can omit `forges:` entirely — a default
GitHub entry is synthesized from `github_token`/`github_username`.

### Point a project at a non-GitHub forge

```yaml
# config/active/projects/codeberg-myproject.yaml
owner: "myorg"
repo: "myproject"
forge: "codeberg"          # references forges[].name above
review_context: "..."
governance: "personal"
```

For GitLab, `owner` is the namespace path (e.g. `myorg` or
`myorg/subgroup`) and `repo` is the project slug. The PR number frank
displays for a GitLab project is the MR's project-internal `iid`.

### Known v1 limitations

- **Gitea/Forgejo recall:** the API path used to delete a posted review
  comment varies between Gitea versions. Recall is best-effort.
- **GitLab review grouping:** GitLab has no "review object" that bundles
  inline comments together; each comment is posted as its own
  discussion. The dashboard still groups by draft, the wire format just
  differs.
- **Mock mode** (`mock_mode: true`) ships only GitHub-shaped fixtures.
  Forgejo/Gitea/GitLab project configs work in live mode but fall back
  to GitHub-shaped demo data offline.

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

All configuration lives in `config/active/operator.yaml` — the single source of
truth.  Secrets (API keys, tokens) go in `.env` and are referenced via `${VAR}`
syntax in the YAML config.

The app auto-detects it: if `config/active/operator.yaml` exists, it's used;
otherwise `config/examples/` is used as a fallback.

String values in YAML support `${ENV_VAR}` expansion, e.g.:
```yaml
github_token: "${FRANK_GITHUB_TOKEN}"
data_dir: "${HOME}/frank-data"
```

Restart services after config changes.

## Mock mode vs real mode

| | Mock mode (`mock_mode: true`) | Real mode (`mock_mode: false`) |
|---|---|---|
| **Tokens needed** | None | `FRANK_GITHUB_TOKEN` + LLM API key (in `.env`) |
| **Data source** | Fixture JSON in `config/fixtures/` | Live GitHub API |
| **Good for** | Demo, development, testing | Actual PR review |
| **Default** | No (set `mock_mode: true` in operator.yaml) | Yes |

Mock mode is the default so you can try the dashboard immediately without any
API keys.

## Troubleshooting

**Port 8000 already in use** — Another service is using it. Either stop that
service or change the port: `python manage.py runserver 8001` (or edit
`compose.yaml` ports).

**Worker not picking up PRs** — Check that `mock_mode: false` in `operator.yaml`
and `FRANK_GITHUB_TOKEN` is set in `.env`. The worker polls every
`poll_interval_seconds` (default 300). Restart the worker to trigger an immediate poll.

**`make setup` fails on missing Python** — You need Python 3.11+. Check with
`python3 --version`. On macOS: `brew install python@3.12` (you may also need
`xcode-select --install` first for headers). On Ubuntu:
`sudo apt install python3.12 python3.12-venv`.

**Docker build fails** — Make sure Docker is running. Try `docker compose build
--no-cache` for a clean build.

## Logging

### Where logs go

All log output goes to **stdout/stderr**:

- **Docker Compose:** `docker compose logs -f worker` / `docker compose logs -f web`
- **Make / local:** logs print directly to the terminal that's running the worker

### Changing the log level

Set `log_level` in `config/active/operator.yaml`:

```yaml
# CRITICAL | ERROR | WARNING | INFO | DEBUG | NOTSET
log_level: "INFO"   # default
```

Or override at runtime without touching the file:

```bash
# env var — works for both Docker and local
FRANK_LOG_LEVEL=DEBUG make worker

# CLI flag (worker only)
make worker-debug          # shortcut for --log-level=DEBUG
python manage.py run_worker --log-level=DEBUG
python manage.py run_worker --debug   # same thing
```

Use `DEBUG` when diagnosing issues — it includes prompt sizes, token counts, and
the raw JSON returned by LLM backends.

### Common log messages from LLM backends

The worker logs a clear message whenever an AI backend call fails.

| Message | What it means | How to fix |
|---|---|---|
| `HTTP 401 … authentication failed` | API key rejected | Check that the key in `.env` is correct and not expired |
| `HTTP 403 … permission denied` | Key lacks permissions or account suspended | Check your provider account dashboard |
| `HTTP 429 … rate limited` | Quota exhausted for this billing cycle | Wait for the quota to reset, or upgrade your plan; franktheunicorn will retry automatically on the next poll |
| `HTTP 4xx` (other) | Bad request — likely a model or config problem | Check `log_level: "DEBUG"` for the full error body |
| `API call failed` (no HTTP code) | Network or SDK error | Check your internet connection and that the provider's API is reachable |

All 4xx messages are logged at `ERROR` level except 429 (rate limit), which is
`WARNING` because it is transient and handled automatically.
