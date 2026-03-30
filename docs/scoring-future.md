# Scoring: Future Work (v1.5+)

Deferred from the v1 scoring implementation. Circle back to these.

## Collaborator Detection Enhancements (§2.4)

- **co_file_committers** (weight 25): Git log analysis — authors who frequently commit to the same files as operator within last 6 months. Requires `data_access/git/` module.
- **co_authors** (weight 20): Parse `Co-authored-by` trailers from git log. Requires git log data access.
- **mailing_list_interaction** (weight 5): Community sources where both operator and person participate. Requires `data_access/mailing_list/` module.
- **CLI**: `review-agent detect-collaborators --project spark` for auto-detection with merge into collaborators file.

## Custom Scoring from .py Files (§2.9)

- LLM-generated `custom_score(pr_data) -> float` functions saved to `~/.review-agent/projects/<project>.scoring_fn.py`
- `review-agent regenerate-scoring --project spark` CLI command
- `--preserve-edits` flag to merge manual edits with regenerated code
- Human review required before activation

## Committer-Is-On-It Deranking (§2.7)

- Detect when another project committer is actively reviewing a PR
- -25 point adjustment when: committer posted review in last 48h AND PR not in operator's watch_paths AND operator not @-mentioned
- Requires committer list data access (GitHub team endpoint or COMMITTERS file)

## Workspace Mode (§2.8)

- Named sets of projects (work, personal, all)
- Dashboard workspace selector with keyboard shortcut `W`
- Per-workspace digest scheduling
- Separate PR, not scoring module concern

## Shepherding Mode (§2.3 Details)

- v2: Draft responses to reviewer questions on operator's own PRs
- Auto-rebase when base branch diverges
- Suggested follow-up actions
- Dashboard section: "Your PRs Needing Action"

## LLM Worker Integration

- Prompt template for LLM vibes scoring (the scoring module just consumes the judgment string)
- Worker calls LLM with operator profile + truncated diff
- Configurable per-project: `llm_scoring_enabled: true`
- Cost consideration: only call for PRs above a score threshold
