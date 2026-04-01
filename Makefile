.DEFAULT_GOAL := help

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Auto-detect if venv exists; if not, use system python for venv creation.
ifeq ($(wildcard $(VENV)/bin/python),)
  ACTIVATE_MSG := "(run 'make venv' first or 'make setup')"
endif

.PHONY: help venv setup test lint format typecheck check serve worker migrate docker-up docker-build clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/python:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

venv: $(VENV)/bin/python ## Create virtual environment if it doesn't exist

setup: venv ## One-time local development setup (creates venv + installs deps)
	@if [ ! -f .env ]; then \
		echo "\033[33mWarning: .env file not found. Run 'cp .env.example .env' and edit it.\033[0m"; \
	fi
	$(PIP) install -e ".[dev]"
	$(PYTHON) manage.py migrate

test: venv ## Run tests with coverage
	$(PYTHON) -m pytest --cov=franktheunicorn --cov-report=term-missing

lint: venv ## Check linting and formatting
	$(VENV)/bin/ruff check src/ tests/
	$(VENV)/bin/ruff format --check src/ tests/

format: venv ## Auto-format code
	$(VENV)/bin/ruff format src/ tests/

typecheck: venv ## Run mypy type checking
	$(PYTHON) -m mypy src/franktheunicorn/

check: lint typecheck test ## Run all checks (lint + typecheck + test)

serve: venv ## Start Django dev server
	$(PYTHON) manage.py runserver

worker: venv ## Start background worker
	$(PYTHON) -m franktheunicorn.worker.runner

migrate: venv ## Run database migrations
	$(PYTHON) manage.py migrate

docker-up: ## Start all services with Docker Compose
	docker compose up

docker-build: ## Build Docker images
	docker compose build

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov dist build *.egg-info coverage.xml .coverage
