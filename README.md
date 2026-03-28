# 🦄 franktheunicorn

Local-first, cloneable AI code review assistant for open-source maintainers.

Monitors PRs across multiple projects, triages by relevance, drafts review comments in your voice, learns from your feedback, and surfaces what needs attention via a lightweight dashboard.

**This is NOT a hosted service.** Clone it, configure it for your repos, run it on your own machine. State lives in SQLite and local files.

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/franktheunicorn/openunicorn.git
cd openunicorn
docker compose up
```

Dashboard at [http://localhost:8000](http://localhost:8000). Mock mode is on by default — no GitHub token needed to try it out.

### Local Development

```bash
git clone https://github.com/franktheunicorn/openunicorn.git
cd openunicorn
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python manage.py migrate
python manage.py runserver          # Dashboard
python -m franktheunicorn.worker.runner  # Worker (in another terminal)
```

### With Real GitHub Data

```bash
export FRANK_GITHUB_TOKEN=ghp_your_token_here
export FRANK_MOCK_MODE=false
docker compose up
```

## Configuration

- **Operator config**: `configs/examples/operator.yaml` — your GitHub username, review style, polling interval
- **Project configs**: `configs/examples/projects/*.yaml` — one file per repo with watched paths, tone, frequent contributors

## Architecture

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

## Testing

```bash
pytest                                    # Run all tests
pytest --cov=franktheunicorn              # With coverage
pytest -m integration                     # Integration tests only
ruff check src/ tests/                    # Linting
mypy src/franktheunicorn/                 # Type checking
```

## Core Principles

1. **Local-first.** SQLite + local files. Back it up, rsync it, check it into git.
2. **Clone and run.** Working dashboard in five minutes.
3. **Operator-in-the-loop.** The agent drafts; you post.
4. **Project-aware.** Each repo has its own review norms and context.
5. **Learns from corrections.** Rejected/edited comments improve future drafts.

## License

Apache License 2.0