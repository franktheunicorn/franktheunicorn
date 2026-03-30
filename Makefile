.DEFAULT_GOAL := help

.PHONY: help setup test lint format typecheck check serve worker migrate docker-up docker-build clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

setup: ## One-time local development setup
	scripts/dev_setup.sh

test: ## Run tests with coverage
	pytest --cov=franktheunicorn --cov-report=term-missing

lint: ## Check linting and formatting
	ruff check src/ tests/
	ruff format --check src/ tests/

format: ## Auto-format code
	ruff format src/ tests/

typecheck: ## Run mypy type checking
	mypy src/franktheunicorn/

check: lint typecheck test ## Run all checks (lint + typecheck + test)

serve: ## Start Django dev server
	python manage.py runserver

worker: ## Start background worker
	python -m franktheunicorn.worker.runner

migrate: ## Run database migrations
	python manage.py migrate

docker-up: ## Start all services with Docker Compose
	docker compose up

docker-build: ## Build Docker images
	docker compose build

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov dist build *.egg-info coverage.xml .coverage
