# franktheunicorn

A **local-first, cloneable AI code review assistant** for open-source maintainers.

Monitors pull requests across multiple GitHub projects, scores them by relevance, drafts review comments in your voice, and shows what needs attention in a lightweight dashboard.

**This is NOT a hosted service.** You clone it, configure it for your repos, and run it locally. State lives in SQLite. No SaaS account, no multi-tenant architecture, no uptime assumptions.

---

## Quick Start

```bash
git clone https://github.com/franktheunicorn/openunicorn
cd openunicorn

# Copy and edit the example configs.
cp configs/examples/operator.yaml configs/operator.yaml
mkdir -p configs/projects
cp configs/examples/project-myapp.yaml configs/projects/myapp.yaml
# Edit configs/operator.yaml with your GitHub login.
# Edit configs/projects/myapp.yaml for your repo.

# Set your GitHub token.
export FRANK_GITHUB_TOKEN=ghp_...

# Start everything.
docker compose up
```

Dashboard: http://localhost:8000

---

## Architecture

```
franktheunicorn/
├── src/franktheunicorn/     # Shared application package
│   ├── config.py            # Operator + project config (YAML)
│   ├── models.py            # SQLAlchemy ORM models
│   ├── database.py          # DB engine + session management
│   ├── github_client.py     # GitHub REST API client (httpx)
│   ├── scoring.py           # PR interest scoring
│   ├── review.py            # Stub review draft generator
│   ├── storage.py           # CRUD helpers
│   ├── anti_pattern.py      # Anti-pattern store + Bayesian suppression
│   ├── poller.py            # GitHub polling service
│   └── digest.py            # Daily digest plumbing (stub)
├── web/                     # FastAPI dashboard
│   ├── main.py
│   └── templates/dashboard.html
├── worker/                  # Polling worker
│   └── main.py
├── tests/                   # pytest test suite (>75% coverage)
├── configs/
│   ├── examples/            # Example YAML configs
│   └── projects/            # Your per-project configs (gitignored)
├── data/                    # SQLite DB + local state (gitignored)
├── scripts/                 # Dev helpers
├── Dockerfile.web
├── Dockerfile.worker
├── compose.yaml
└── pyproject.toml
```

### Services

| Service | Description |
|---------|-------------|
| `web` | FastAPI dashboard at :8000 |
| `worker` | Polls GitHub on a configurable interval |

Both services share the same SQLite database via a mounted volume.

---

## Configuration

### Operator config (`configs/operator.yaml`)

```yaml
github_login: franktheunicorn
email: frank@example.com
trusted_collaborators:
  - alice
  - bob
stale_pr_days: 30
```

### Project config (`configs/projects/myapp.yaml`)

```yaml
slug: myapp
repo: franktheunicorn/myapp
review_context: |
  Small personal Django app. Pragmatic review style.
watched_paths:
  - "myapp/**"
  - "requirements*.txt"
frequent_contributors: []
asf_project: false
poll_interval_seconds: 300
max_prs_per_poll: 20
enabled: true
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FRANK_GITHUB_TOKEN` | (required) | GitHub personal access token |
| `FRANK_DATABASE_URL` | `sqlite:///./data/franktheunicorn.db` | SQLite DB path |
| `FRANK_OPERATOR_CONFIG_PATH` | `configs/operator.yaml` | Operator config file |
| `FRANK_PROJECTS_CONFIG_DIR` | `configs/projects` | Project configs directory |
| `FRANK_LOG_LEVEL` | `INFO` | Log level |

---

## PR Scoring

Each PR gets an interest score based on signals:

| Signal | Delta |
|--------|-------|
| Operator is the PR author | +1.0 |
| Operator mentioned / review-requested | +0.8 |
| PR touches a watched path | +0.6 |
| Author is a frequent/trusted contributor | +0.4 |
| New contributor (first-timer) | +0.3 |
| Likely AI-generated / low-context | -0.3 |
| Stale PR | -0.1 |

---

## Development

```bash
bash scripts/dev_setup.sh

# Run tests.
pytest

# Lint + type check.
ruff check .
mypy src/franktheunicorn web worker

# Start web service only.
uvicorn web.main:app --reload

# Start worker (one-shot).
python -m worker.main
```

---

## Design Principles

- **Local-first**: SQLite + local files. No Postgres, no Redis, no cloud.
- **Clone and run**: `git clone`, set env vars, `docker compose up` - working in ~5 minutes.
- **Operator-in-the-loop**: the agent drafts, you post. Auto-posting is a future extension.
- **Project-aware**: each repo has its own review norms and scoring context.
- **Learns from corrections**: anti-patterns from negative feedback suppress future similar suggestions.
- **Boring stack**: FastAPI, SQLAlchemy, Alembic, Pydantic, httpx, pytest. Nothing exotic.
