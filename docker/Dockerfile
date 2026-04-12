FROM python:3.12-slim AS base

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (copy pyproject.toml + README first for layer caching)
COPY pyproject.toml README.md ./
RUN mkdir -p src/franktheunicorn && \
    touch src/franktheunicorn/__init__.py && \
    pip install --no-cache-dir . && \
    rm -rf src/franktheunicorn

# Copy application source
COPY src/ src/
COPY manage.py .
RUN pip install --no-cache-dir --no-deps .

# Copy configs (can be overridden by volume mounts)
COPY config/ config/

# Copy docker templates so setup_llm --docker can generate compose overrides
COPY docker/ docker/

# Ensure data dir exists
RUN mkdir -p /app/data

FROM base AS web
EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

FROM base AS worker

# The worker needs git for repo management and blame analysis.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Optionally install the CodeRabbit CLI (v1.5 feature). Set
# `--build-arg INSTALL_CODERABBIT=true` to bake it into the image.
# See https://docs.coderabbit.ai/cli for details. CodeRabbit degrades
# gracefully at runtime when the CLI is not installed.
ARG INSTALL_CODERABBIT=false
RUN if [ "$INSTALL_CODERABBIT" = "true" ]; then \
        curl -fsSL https://cli.coderabbit.ai/install.sh | sh; \
    fi

CMD ["python", "-m", "franktheunicorn.worker.runner"]
