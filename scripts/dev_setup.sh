#!/usr/bin/env bash
# dev_setup.sh – Bootstrap local development environment.
# Usage: bash scripts/dev_setup.sh
set -euo pipefail

echo "Installing franktheunicorn in editable mode with dev extras …"
pip install -e ".[dev]"

echo "Creating data/ directory if needed …"
mkdir -p data configs/projects

echo "Copying example configs if no operator.yaml present …"
if [[ ! -f configs/operator.yaml ]]; then
  cp configs/examples/operator.yaml configs/operator.yaml
  echo "  Created configs/operator.yaml from example.  Edit before running."
fi

echo ""
echo "Setup complete.  Next steps:"
echo "  1. Edit configs/operator.yaml with your GitHub login."
echo "  2. Copy and edit a project config into configs/projects/."
echo "  3. Set FRANK_GITHUB_TOKEN in your shell or .env file."
echo "  4. Run: uvicorn web.main:app --reload"
echo "     Run: python -m worker.main"
echo "  Or:  docker compose up"
