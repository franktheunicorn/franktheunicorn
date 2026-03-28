#!/usr/bin/env bash
# reset_db.sh – Drop and recreate the local SQLite database.
# Usage: bash scripts/reset_db.sh
set -euo pipefail

DB_PATH="${FRANK_DATABASE_URL:-sqlite:///./data/franktheunicorn.db}"
DB_FILE="${DB_PATH#sqlite:///}"

if [[ -f "$DB_FILE" ]]; then
  echo "Removing $DB_FILE …"
  rm "$DB_FILE"
fi

echo "Running Alembic migrations …"
alembic upgrade head

echo "Done. Fresh database at $DB_FILE"
