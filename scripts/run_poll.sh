#!/usr/bin/env bash
# Run a single polling cycle (useful for testing without the full worker loop).

set -euo pipefail

cd "$(dirname "$0")/.."

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-franktheunicorn.settings}"
export FRANK_MOCK_MODE="${FRANK_MOCK_MODE:-true}"

python -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'franktheunicorn.settings')
django.setup()

from django.conf import settings
from franktheunicorn.backends.mock import MockForgeClient
from franktheunicorn.backends.poller import poll_project
from franktheunicorn.config.loader import load_operator_config, load_project_configs
from franktheunicorn.review.drafter import draft_review

operator = load_operator_config(settings.FRANK_OPERATOR_CONFIG)
projects = load_project_configs(settings.FRANK_PROJECTS_DIR)
client = MockForgeClient(settings.FRANK_FIXTURES_DIR)

for pc in projects:
    if pc.enabled:
        prs = poll_project(client, pc, operator.github_username)
        for pr in prs:
            if not pr.review_drafts.exists():
                drafts = draft_review(pr, pc)
                print(f'  PR #{pr.number}: score={pr.interest_score:.2f}, {len(drafts)} drafts')
        print(f'Polled {pc.full_name}: {len(prs)} PRs')

print('Done!')
"
