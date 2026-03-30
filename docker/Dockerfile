FROM python:3.12-slim AS base

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (copy pyproject.toml first for layer caching)
COPY pyproject.toml .
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

# Ensure data dir exists
RUN mkdir -p /app/data

FROM base AS web
EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

FROM base AS worker
CMD ["python", "-m", "franktheunicorn.worker.runner"]
