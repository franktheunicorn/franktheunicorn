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
    pip install --no-cache-dir ".[dev]" && \
    rm -rf src/franktheunicorn

# Copy application source
COPY src/ src/
COPY manage.py .
RUN pip install --no-cache-dir --no-deps ".[dev]"

# Copy configs (can be overridden by volume mounts)
COPY config/ config/

# Copy docker templates so setup_llm --docker can generate compose overrides
COPY docker/ docker/

# Ensure data dir exists
RUN mkdir -p /app/data

FROM base AS web
EXPOSE 8000
# Collect static files at image-build time so WhiteNoise can serve them.
# The runserver dev path doesn't need this; the gunicorn production path
# does. Run with DJANGO_DEBUG=false at runtime in the compose file to
# pick up the WhiteNoise middleware (only inserted when DEBUG is off).
RUN python manage.py collectstatic --noinput || true
CMD ["gunicorn", "franktheunicorn.wsgi:application", \
     "--bind", "0.0.0.0:8000", "--workers", "2", \
     "--worker-class", "gthread", "--threads", "4", \
     "--timeout", "90", "--graceful-timeout", "30", \
     "--max-requests", "1000", "--max-requests-jitter", "100", \
     "--access-logfile", "-"]

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
