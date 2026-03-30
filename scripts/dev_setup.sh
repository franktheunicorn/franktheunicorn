#!/usr/bin/env bash
# Local dev setup script.
# Run this once after cloning to set up the development environment.

set -euo pipefail

echo "🦄 Setting up franktheunicorn development environment..."

# Create virtualenv if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Creating virtualenv..."
    python3.12 -m venv .venv
fi

echo "Activating virtualenv..."
source .venv/bin/activate

echo "Installing dependencies..."
pip install -e ".[dev]"

echo "Creating data directory..."
mkdir -p data

echo "Running migrations..."
python manage.py migrate

echo ""
echo "✅ Setup complete!"
echo ""
echo "Quick start:"
echo "  source .venv/bin/activate"
echo "  python manage.py runserver          # Start web dashboard"
echo "  python -m franktheunicorn.worker.runner  # Start worker"
echo ""
echo "Or use Docker:"
echo "  docker compose up"
