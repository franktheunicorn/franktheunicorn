# Apache Magpie and franktheunicorn

> **Status:** Living document. First written 2026-10-12 against Magpie commit
> `dadb315caa8b881fc1d2195bef03223dac25915a` (2026-10-11).
>
> **Audience:** The operator deciding what to borrow from Magpie, and the agents
> implementing it. Every claim names a file on both sides so it can be re-checked
> after either project moves.

---

## Table of Contents

- [1. Summary and Positioning](#1-summary-and-positioning)
- [2. How We Pin Magpie](#2-how-we-pin-magpie)
- [3. Where Each Piece Runs](#3-where-each-piece-runs)
- [4. Tier 1: Adopt Now](#4-tier-1-adopt-now)
- [5. Tier 2: Adopt Later](#5-tier-2-adopt-later)
- [6. Tier 3: Borrow the Text or the Design](#6-tier-3-borrow-the-text-or-the-design)
- [7. Interop and Contributions Upstream](#7-interop-and-contributions-upstream)
- [8. Considered and Rejected](#8-considered-and-rejected)
- [9. Open Questions](#9-open-questions)
- [10. Sources](#10-sources)

---

## 1. Summary and Positioning

Apache Magpie is the ASF's framework for agent-assisted maintainership. It
graduated to a Top-Level Project on 17 June 2026 and grew out of the
`apache/airflow-steward` security-report tooling. It ships as a library of
markdown skills that a maintainer invokes inside a coding agent (Claude Code,
Codex, Cursor, Gemini CLI, Copilot) plus small stdlib Python tools, organised
into families: security, pr-management, issue, mentoring, contributor-growth,
release-management, repo-health, pairing, setup and utilities. Its mission
document names five modes (Agentic Triage, Mentoring, Drafting, Pairing,
Autonomous). Its `docs/modes.md` marks Triage and Drafting stable for security
work only and experimental elsewhere, Mentoring and Pairing experimental, its
framework machinery stable, and Autonomous off by design. Its state lives in the forge: a private tracker repository's issues,
labels and a status rollup comment. Its safety posture is the part frank should
take most seriously: external content is data and never an instruction; outputs
are probabilistic and gates are deterministic; every agent-authored output is a
proposal a human signs; no telemetry by default (opt-in per project); a
committed lock plus gitignored snapshot instead of vendored copies.

frank is a different shape of tool for an overlapping job. It is an always-on
worker with its own SQLite state and a dashboard, it scores and routes PRs by
the operator's interest, it drafts findings in the operator's voice, and it
learns from corrections through the anti-pattern list. Magpie has none of that:
no daemon, no scoring, no voice, no learning loop. frank has none of Magpie's
release management, mentoring or contributor-growth work, and does not want it.

Where the two overlap, Magpie is usually ahead on **rules** and frank on
**plumbing**:

| Area | Magpie has | frank has |
|------|------------|-----------|
| Security reports | six triage dispositions, CVE 5 record builders with CWE, credits and reference tags, reporter-facing canned wording, PoC and confidentiality policies, a deterministic security-language guard for fix branches | SQLite backlog, LLM triage staged for the operator, NVD lookup, backlog dedup, per-branch agent verification, git-only fix-landed proofs, CSV sheet sync, a thin ASF CVE service client |
| PR review | review criteria files, deterministic scans (undisclosed security fix, AI authorship, licence headers, compiled artifacts), CODEOWNERS grounding, posting discipline (no live @-mentions, anchor validation, head-sha guard) | interest scoring, blame, anti-patterns, tone guard, prior-comment dedup, multi-reviewer dedup, copypasta detection |
| Agent confinement | per-CLI read-only argv, credential-stripped env, PreToolUse guard, sandbox settings | containers for tests and PoC runs; on this branch, read-only argv for all four seeded CLIs (4.11), the checkout's own agent config moved aside for every run (4.18), and contributor text fenced in every prompt (4.4) |
| Intake | ASF relay and GHSA forwarder detection, IMAP thread reconstruction, PII redactor | IMAP fetcher, regex injection scanner, scanner-archive import |

So the strategy is: take Magpie's rules and text as pinned prompt material or
small deterministic ports, pin the two or three stdlib tools worth calling by
commit sha, never run Magpie's interactive skills headless inside the worker
(they assume `gh`, a tracker repository and a maintainer at the keyboard), and
contribute upstream where frank is ahead. Everything frank takes is tied to one
upstream commit (section 2) and placed against the remote execution model
(section 3).

Principles frank already follows and should name: human in the loop (draft-only
posting), deterministic gates around probabilistic output (the review queue),
no telemetry (local-first; but see question 2 in section 9), audit of every
posted action (operator actions and the 24h recall window, which covers inline
comments). Two it adopted on this branch: external content is data in every
prompt that carries contributor or reporter text (4.4), and tracker
identifiers are public-safe but contents are not (the CVE record no longer
cites a private thread). One principle frank cannot adopt as written:
contributor sentiment gating mode graduation needs a community measuring it,
and frank is a single operator's tool.

---

## 2. How We Pin Magpie

Magpie lands many commits a day. It has shipped exactly one release, `0.1.0`
(tag `1890a13d0d3a3b0b2a9c83efb7dcdc837a07795d`, 2026-07-13, announced
2026-08-03), and most of what frank wants did not exist at that tag:

| Piece | In `0.1.0`? | Commits since the tag |
|-------|-------------|-----------------------|
| `tools/privacy-llm` | yes | 11 |
| `tools/agent-guard` | yes | 25 |
| `tools/cve-tool-vulnogram/generate-cve-json`, `oauth-api` | yes | 13 each |
| `tools/adversarial-review` | no | 8 |
| `tools/pr-management` | no | 10 |
| `plugins/magpie-pr-management/skills/code-review` | no | 20 |
| `plugins/magpie-security/skills/issue-triage` | no | 16 |

So the pin is a commit, not a tag, and it is recorded once:

- **The lock** is `src/franktheunicorn/magpie/magpie-pin.yaml`: the upstream
  URL, one full 40-hex commit, the tag that points at it when one does, the
  commit date, the list of pinned Python tools, and one entry per vendored file
  with the sha256 of the upstream bytes.
- **Vendored text** would live in `src/franktheunicorn/magpie/vendored/`. The
  lock lists none today: every piece adopted so far was ported into frank's
  own modules with the upstream path and commit cited (section 4), because
  none needed Magpie's words verbatim. A vendored file opens with a
  provenance header naming the upstream path and commit and keeps Magpie's
  own SPDX line in its body. `vendored_text()` in
  `src/franktheunicorn/magpie/pin.py` returns the body without the header, so
  prompts never carry the comment, and `--check` guards the first entry.
- **Worker-side Python tools** are installed by `pip` from the git URL at the
  pinned sha plus `#subdirectory=tools/<name>`. `PinnedTool.requirement()`
  renders the exact string. They would live in an optional dependency group
  so a default install pulls nothing from GitHub. None is pinned yet; 5.5 is
  the first candidate.
- **Agent-host tools** are run with `uvx --from git+<url>@<sha>#subdirectory=...`
  built by `PinnedTool.uvx_argv()`. That is one argv with no pre-install step,
  which is what the remote executor can carry (section 3).

The commands, all under `manage.py magpie_sync`:

```bash
python manage.py magpie_sync --check          # offline; CI runs this (make magpie-check)
python manage.py magpie_sync --sync           # re-fetch the pinned commit, regenerate vendored/
python manage.py magpie_sync --bump <40-hex>  # re-point the lock, sync, print the compare URL
```

A bump is reviewed as the diff it is: the command prints
`https://github.com/apache/magpie/compare/<old>...<new>`, the reviewer reads that
range for the paths the lock names, and the PR carries the regenerated vendored
files so the text change is visible in review. `--check` fails on a vendored
file whose body no longer hashes to the lock, whose header names another
commit, or that is not in the lock at all, so a hand edit or a half-done bump
cannot ship.

Why nothing floats: a floating ref would make frank's prompts change under the
operator without a commit in frank, which is the one thing the anti-pattern
learning loop cannot explain. Magpie's own adopters get the same discipline from
`.apache-magpie.lock`; this is the same idea with frank's packaging.

---

## 3. Where Each Piece Runs

The maintainer's requirement, in their words: "make sure that our magpie
integration works with our weird remote ssh thing for calling models we don't
have tokens per-se necessarily."

The maintainer's deployment runs the agent CLIs (`cursor-agent`, `claude`,
`codex`, `pi`) on another host over SSH. frank holds no API tokens for those
models; auth is the CLI's own login on that host. Some SSH wrappers
(`sf workspace ssh --no-et`) have no remote-command form at all, so frank drives
them by piping a framed script into the interactive shell they open. The
`agent-cli` LLM backend sends ordinary LLM calls (security triage, shepherding)
through the same CLIs. The executor is `src/franktheunicorn/review/tool_executor.py`
(`RemoteSSHExecutor`: `prepare_repo`, `run`, and `run_script`) and the config is
`RemoteExecutionConfig` in `src/franktheunicorn/config/models.py`. Every Magpie
piece has to be placed against that model.

| Location | What runs there | Reaches it how |
|----------|-----------------|----------------|
| Worker host (and the web process, usually the same host) | frank's Python: prompt building, parsing, triage, dedup, posting, lock checks | ordinary imports; pinned tools via `pip` |
| Remote agent host | the agent CLI and `git` inside a checkout under `remote_workspace_dir` | one shell invocation per call through `executor.run()` or `run_script()`; in stdin delivery mode a process stdin payload is dropped and output is capped at 4 MB |
| The model | prompt text | argv; stdin delivery base64-stages a long line, so the tty's 4 KB line limit does not apply, but one argv element is still bounded by the OS (under 128 KiB on Linux) |

### What fits, and what does not

What fits that model, verified on this box:

1. **Prompt text** in frank's own templates needs nothing anywhere and works with
   every CLI and every wrapper. This is the default for anything that is a rule,
   a rubric or a checklist.
2. **Worker-side `pip` by sha** for stdlib-only tools frank calls on its own
   host, before a prompt leaves it or after a result comes back.
3. **`uvx` from the pinned sha** on the agent host, for a tool that must run next
   to the agent. Only `uv` has to exist there; the first run builds and caches.
   `uvx --from "git+https://github.com/apache/magpie.git@dadb315caa8b881fc1d2195bef03223dac25915a#subdirectory=tools/agent-guard" agent-guard`
   was run here end to end.
4. **A pinned snapshot on the agent host** when a tool must be a file path or
   `uv` is absent: `git init`, `remote add`,
   `fetch --depth 1 --filter=blob:none origin <sha>`, `checkout FETCH_HEAD`, in
   about a second (61 MB full, 6 MB as a sparse checkout of the four directories
   frank reads). Magpie's stdlib tools then run with plain `python3` from the
   snapshot through `run_script`. This is no longer a route for running Magpie
   *skills* in a review lane. The agent-config guard (4.18) moves `.claude` aside
   for the length of every agent run, so a `.claude/skills/magpie-<skill>` link
   is gone while the agent runs. The read-only argv (4.11) gives the claude and pi
   reviewers no shell and no Skill tool, and holds cursor-agent to `--mode ask`
   and codex to its read-only sandbox. A skill whose body opens with Magpie's
   `setup_preflight` block (`python3 -m setup_preflight`, then
   `/magpie-setup config` when the checkout has no Magpie setup) therefore
   cannot run, even where `.agents/skills` makes it visible.

What does not fit:

- **Claude Code marketplace plugins.** They are per-machine `~/.claude/plugins`
  state installed by `/plugin` or `claude plugin` commands, and they reach `claude`
  only, never cursor-agent, codex or pi. They cannot be pinned to the one release
  either: tag `0.1.0` has no `.claude-plugin/marketplace.json` and no `plugins/`
  tree, so `/plugin marketplace add apache/magpie@0.1.0` finds no catalogue.
  Magpie's install docs name a `0.2.0` tag that does not exist; the dev line
  moved to 0.9.0 on 2026-09-25 (commit `8ae1427f`).
- **Any skill whose body shells out to `gh`** or waits for a maintainer's answer.
  frank does not assume `gh` on the agent host, the review checkout is not a
  Magpie adopter repo, and nobody answers `[Y]es` in a headless run.
- **`PreToolUse` hooks.** They apply to `claude` sessions only, need `python3` on
  the remote, and a hook file in the checkout is now moved aside by 4.18 anyway.

The degradation rule is the one in CLAUDE.md: a configured Magpie piece that
cannot run on the remote (no `uv`, no `python3`, a failed fetch) logs once at
WARNING naming the host and the setting, and the review proceeds without it.

### Per-piece placement

Every survivor lands in one of three places. Some run on the worker or web host:
frank's own Python, plus a `pip` install by sha for the two optional Magpie
packages. Some reach the remote agent host, but only as plain argv and `git`
through `executor.run()`, or (5.6) a short POSIX `sh` script through
`run_script()` that starts the agent under `env -i`, with nothing installed
there. The rest are prompt text
that rides the CLI's argv to the model. No surviving piece needs `uv`, `python3`,
`gh`, a plugin or a snapshot on the agent host. All of them work with every CLI,
including through wrappers that have no remote-command form. The pieces that do
not fit (marketplace plugins, skill bodies that need `gh` or a person at the
keyboard) are among the skip rows. The one survivor with a negative remote verdict,
running Magpie's code-review skill headless, is a skip for exactly that reason.

"Done" rows name their section 4 entry; other rows name where they are
discussed. "All four" means claude, codex, cursor-agent and pi.

| Id | Status | Runs on | Remote recipe | Remote needs | CLIs | If it cannot run |
|----|--------|---------|---------------|--------------|------|------------------|
| `security-triage-dispositions` | done (4.1) | worker; model | none; when triage falls through to an agent-cli reviewer the prompt rides that CLI's argv | none | all four | no `disposition` key: the old three-way mapping |
| `cve-record-merge-guards` | done (4.2) | web | none | none | none involved | no write token or a failed record read: nothing is pushed and the panel says why; no record on the service yet: the guards pass it through; a refusal names its override or says it has none |
| `fix-agent-security-language-guard` | branch names done (4.3); commit messages in 5.3 | web (branch names); agent host (commit messages) | `executor.run(["git","fetch",...])` then `executor.run(["git","log","--format=%B",...])` in `branch_scan` | `git`, the scan checkout, and network to the git host (all assumed) | independent of the CLI | read fails: WARNING naming the host, no badge rather than a false "clean" |
| `untrusted-content-rule-in-all-prompts` | done (4.4) | model | argv | none | all four and the agent-cli backend | pure string assembly; never raises |
| `persist-injection-hits-at-intake` | done (4.5) | worker; web | none | none | none involved | regex cannot fail; a hit is a WARNING and a callout, never a refusal |
| `import-body-sanitisation-and-poc-policy` | callout done (4.5); sanitiser dropped; policy text in 6.5 | worker; web | none | none | none involved | n/a |
| `asf-relay-detection-and-credit-extraction` | done (4.6) | worker; web | none | none | none involved | no relay match: today's parse |
| `finder-and-bot-credit-policy` | relay credit typing done (4.6); rest in 5.17 | worker | none | none | none involved | timing unknown: `finder`, operator decides |
| `poc-attachments-manifest-and-static-first-policy` | manifest done (4.7); label and policy text in 6.5 | worker; web; sandbox on the worker's Docker | none | none | none involved | metadata only; bytes never stored |
| `port-security-disclosure-language-scan` | done (4.8) | worker | none | none | independent of the CLI | check not enabled for the project: not run |
| `port-mention-escaping-before-post` | done (4.9) | web | none | none | applies to every reviewer's drafts | pure string transform |
| `port-inline-anchor-validation-and-head-guard` | done (4.10) | web | none | none | applies to every reviewer's drafts | head or diff unreadable: posts as before and logs why |
| `agent-cli-readonly-flags` | done (4.11) | agent host | plain argv; a test stages it through stdin delivery | the CLI binary | all four | a CLI that rejects a flag exits non-zero; ERROR with its stderr; that reviewer yields nothing |
| `pre-push-hook-command-parsing` | done (4.12) | developer machine | none | n/a | Claude Code sessions on frank itself | untokenisable command: a text search runs the checks on any `git ... push` or `git ... commit` |
| `agent-cli-runner-process-groups` | done (4.13) | worker (local runs); auth advice in both modes | none | none | all four | no `os.killpg`: plain `subprocess.run` |
| `adopt-post-verify-never-retry` | done (4.14) | web | none | none | none involved | read-back fails: `PostUnverifiedError`, "check the PR first" |
| `claimed-vs-assessed-severity-split` | done (4.15) | worker; web; the verifier prompt is built on the worker | none | none | all four | parse omits severity: the claim stays `unknown` |
| `adopt-maintainer-court-prefilters` | F5c done (4.16); routing half in 5.23; F5a cooldown not taken | worker | none | none | none involved | comment fetch fails: signal off |
| `author-association-as-first-timer-and-committer-signal` | new-contributor half done (4.17); committer half in 5.18 | worker | none | none | none involved | blank association: the old rule |
| `author-association-first-timer-and-bot-list` | done with 4.17; its `^github-actions$` pattern is moot on the REST path (REST reports `github-actions[bot]`, which `.*\[bot\]$` in `BOT_PATTERNS` already matches) | worker | none | none | none involved | blank association: the old rule |
| `vendor-external-content-is-data-prompt-line` | rule done (4.4); phrase list and finding shape in 5.16 | worker; model | argv | none | all four | regex cannot fail |
| `merge-intent-withdrawal-clears-queue` | done (4.19) | worker | none | none | none involved | reviews read fails: stored answer kept |
| `fix-landed-final-release-tags-only` | done (4.20) | agent host runs the existing `git tag --contains`; the worker filters | no new invocation | `git` and the verifier clone (assumed) | independent; uses the verifier reviewer's executor | tag listing fails: `indeterminate` |
| `fix-launch-prior-fix-evidence` | Tier 2 (5.4) | web | none | none | Cursor cloud fix agent (HTTPS), not the SSH CLIs | no signals: panel unchanged; never refuses |
| `generate-cve-json-builders` | Tier 2 (5.5) | web; `pip` by sha into frank's venv | none | none | none involved | not installed: built-in skeleton, INFO once |
| `agent-cli-clean-env` | Tier 2 (5.6) | worker (local); agent host (`env -i` prefix) | script text through `run_script`: `cd <cwd>`, build the kept list with `set --`, then `env -i "$@" <guarded argv>` | POSIX `env` and `sh`; `HOME` passes so CLI logins keep working | all four; `uv run claude` keeps working | INFO naming dropped variables; WARNING naming `env_passthrough` when a run then fails auth-shaped |
| `adopt-criteria-files-and-quoted-rule-contract` | Tier 2 (5.7), criteria files only | worker (default-branch clone); model | block rides the prompt argv | none | all four and the LLM backends | file or clone missing: INFO naming `review_criteria_files`, block omitted |
| `port-codeowners-surface-ownership` | Tier 2 (5.8) | worker; model | block rides argv | none | all four and the LLM backends | no CODEOWNERS: no block; no `read:org`: INFO, direct handles only |
| `gitlab-gitea-paginate-open-prs` | Tier 2 (5.9) | worker | none | none | none involved | a page fails: listing raises, no reap |
| `pip-pr-management-diff-only-scans` | Tier 2 (5.10) | worker; `pip` by sha | none | none | independent of the CLI | extra missing: WARNING once, skipped; non-ASF project: INFO naming `governance`, check skipped |
| `cve-org-publication-check` | Tier 2 (5.11) | web or worker | none | none; HTTPS to `cveawg.mitre.org` from the web or worker host | none involved | 404, 5xx or timeout: "public record: unknown", INFO |
| `fix-fork-starting-ref-preflight` | Tier 2 (5.12) | web | none | `git` and HTTPS on the web host (already used) | Cursor cloud fix agent only | unclassifiable failure: WARNING, launch as today |
| `port-ai-authorship-body-signals` | Tier 2 (5.13) | worker | none | none | none involved | no template: no nit |
| `dedup-signals-ghsa-reporter-axes` | Tier 2 (5.14) | worker; the title sweep goes through the configured backend | argv when that backend is agent-cli | none | all four | no ids: today's blend |
| `disclosure-governance-flags` | Tier 2 (5.15) | web | none | none | none involved | missing timestamps: no badge |
| `port-disposition-gates-for-review-event` | Tier 2 (5.19) | web | none | none | none involved | any input missing: COMMENT suggested |
| `port-slop-structural-signals` | Tier 2 (5.20) | worker | none | none | none involved | no diff: H1 skipped |
| `asvs-scan-format-adapter` | Tier 2 (5.21) | web (upload) | none | none | none involved | no blocks parsed: per-file import, INFO |
| `imap-thread-reconstruction-for-followups` | Tier 2 (5.22) | worker | none | none | none involved | server rejects SINCE: back to `unseen`, WARNING |
| `stale-nudge-then-close-for-consider-closing-queue` | Tier 2 (5.23) | worker | none | none | none involved | comment fetch fails: stored flag kept |
| `skill-evals-harness-for-frank-prompts` | Tier 2 (5.24) | developer or worker host; agent host through frank's own backend | the configured reviewer's existing `executor.run()`, prompt in argv | the CLI binary | any configured reviewer | remote unreachable: recorded as an error, not a failure |
| `cve-title-normalize-cascade` | Tier 2 (5.25) | web | none | none | none involved | under three words: original kept with a warning |
| `fix-prompt-rederive-untrusted-patch` | Tier 3 (6.1) | model (Cursor cloud POST body) | none on any agent host | none | Cursor cloud agents only | no runtime dependency |
| `magpie-principles-map-in-design-doc` | Tier 3 (6.2) | docs | none | none | n/a | n/a |
| `reporter-reply-canned-drafts` | Tier 3 (6.3) | web; a `quotes` key in the triage prompt | argv when triage uses an agent-cli backend | none | all four | template missing: frank's example; drafts are never sent |
| `positioning-statement-readme-and-design-doc` | Tier 3 (6.4) | docs | none | none | n/a | n/a |
| `cost-reporting-shaped-like-mode-economics` | Tier 3 (6.6) | worker (a timing row around each `executor.run()`) | none | none | all four | no usage: a "not metered" line, never $0 |
| `adopt-axis-isolated-review-focus` | Tier 3 (6.7) | model | template rides argv | the CLI binary | all four | unknown focus: `operator.yaml` fails validation at load (ERROR, built-in defaults in force), as any bad `review_focus` does today |
| `ci-hygiene-typos-zizmor-dependabot-lychee` | Tier 3 (6.8) | CI and the developer hook | none | none | n/a | tool missing in the hook: a one-line install notice |
| `advisory-hygiene-gates` | Tier 3 (6.9) | web | none | none | none involved | missing fields listed; push stays behind an override |
| `llm-egress-posture-for-security-reports` | Tier 3 (6.10) | docs; one WARNING at worker start | none | none | covers all four and the agent-cli backend | WARNING once; never a gate |
| `four-layer-model-in-security-design` | Tier 3 (6.11) | docs | none | none | n/a | n/a |
| `threat-model-structure-for-design-doc` | Tier 3 (6.11) | docs | none | none | n/a | n/a |
| `contribute-git-commit-pin-sparse-fetch-and-doc-tag-fix` | Upstream (7.4) | docs | none | none | n/a | n/a |
| `contribute-cursor-pi-backends-and-trust-detection` | Upstream (7.3) | Magpie's repo | none in frank | none | n/a | n/a |
| `contribute-git-proofs-and-dedup-upstream` | Upstream (7.5) | docs | none | none | n/a | n/a |
| `contribute-deterministic-injection-scanner-upstream` | Upstream (7.6) | Magpie's repo; if consumed back, worker-side `pip` by sha | none | none | n/a | n/a |
| `contribute-antipattern-loop-and-prior-comment-plus-one` | Upstream (7.7) | docs; an optional export on the worker | none | none | n/a | n/a |
| `contribute-rejection-derived-anti-patterns-pattern` | Upstream (7.7) | docs | none | none | n/a | n/a |
| `contribute-gitlab-forgejo-review-write-paths` | Upstream (7.8) | docs | none | none | n/a | n/a |
| `scan-archive-json-adapter-upstream` | Upstream (7.9) | docs | none | none | n/a | n/a |
| `skip-magpie-code-review-skill-headless` | skip (8) | nowhere: **remote verdict negative** | none works: Step 0 needs `gh auth status` and collaborator access on the agent host, and every step waits for a maintainer | `gh`, `uv`, `python3`, vetted-ops, an adopter project config, a person | none headless | no wiring fixes it under 4.11's read-only argv; borrowing its rules as prompt text (5.7, 5.10) is the working route |
| `skip-marketplace-plugin-pin-for-worker` | skip (8) | nowhere | marketplace plugins reach claude only, mutate the remote `~/.claude`, and have no manifest at `0.1.0` | n/a | claude only | prompt text or a sha snapshot instead |
| `ponymail-mcp-adapter-skip` | skip (8) | nowhere | per-machine `claude mcp add`, Node 20+, a browser LDAP login on the agent host | n/a | claude only | frank's lists.apache.org fetcher and IMAP keep covering it |
| `skip-security-issue-fix-skill-in-cursor-cloud-agent` | skip (8) | nowhere | the Cursor VM is outside frank's executor and has no `gh`, tracker or person | n/a | n/a | its rules are taken as 5.4, 5.12, 6.1 |
| `attacker-text-argv-rule-skip` | skip (8) | n/a | every remote argv element is `shlex.quote`d before a remote shell parses it | a POSIX login shell on the agent host (assumed) | all four | a non-POSIX login shell would misparse; see section 8 |
| `container-gateway-skip` | skip (8) | n/a | in ssh mode the agent host has no frank Docker socket | none | all four | in compose local mode run agent CLIs over ssh or in a sidecar |
| `typed-decision-skip` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `tracker-stats-lifecycle-categories` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-pip-install-privacy-llm-packages` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-mail-source-contract-gmail-ponymail-mcp-mail-patch` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-triage-stale-quickmerge-mutations` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-stats-dashboard-and-triage-markers` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-reviewer-roster-routing-typed-decision-pre-first-pr` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-contribute-tone-copypasta-blame` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-stack-review` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `vetted-ops-skip` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `agent-isolation-interactive-helpers-skip` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-contributor-metrics-as-pip-dependency` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-governance-issue-lifecycle-and-audit-skills` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `sourcehut-fossil-skip` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `asf-svn-nexus-release-tooling-skip` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `safe-redirect-handlers-skip` | skip (8) | n/a | none | none | n/a | n/a |
| `skip-trusted-skill-source-interop` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-two-lock-drift-symlink-relay-model` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-skill-token-count` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-pilot-report-template` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-magpie-adopter-or-skill-source` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-sembr-and-doc-style` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-vote-tally-roster-pass-rule-and-deliberate-divergences` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-release-management-lifecycle-skills` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-pip-release-verify-and-release-config` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-announce-prepare-audit-security-adjacent-rules` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-issue-fix-tracker-update-step10` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-issue-fix-local-clone-step4` | skip (8) | nowhere | none | n/a | n/a | n/a |
| `skip-fixing-security-issues-overview-doc` | skip (8) | nowhere | none | n/a | n/a | n/a |

---

## 4. Tier 1: Adopt Now

Each entry survived two independent adversarial reads (fit and value; pinning
and remote execution) with a mean score of 4 or better and small or medium
effort. "Done" means it is on this branch with tests.

### 4.1 Six triage dispositions (done)

- **What.** The triage prompt asks for a `disposition` (VALID,
  DEFENSE-IN-DEPTH, INFO-ONLY, INVALID, PROBABLE-DUP, FIX-ALREADY-PUBLIC,
  UNCERTAIN) and a `model_quote`, the verbatim line of the security model the
  call rests on. The class is staged on `SecurityReport.auto_triage_disposition`
  and the suggested status follows it; `defense-in-depth` and `info-only` are
  new operator statuses. Info-only owes no fix; defense-in-depth owes an
  ordinary one with no CVE.
- **Why.** frank's three-way split (plausible, expected behaviour, invalid)
  had no name for the most common shape in a real backlog: the code does what
  the report says, and the attack sits outside the security model. Magpie's
  taxonomy was built on exactly that backlog.
- **Files.** `security/prompt.py` (rubric, adapted with attribution),
  `security/triage.py`, `core/models.py` (migration 0059),
  `security/learning.py`, the verdict and triage-result templates.
- **Upstream source.** `plugins/magpie-security/skills/issue-triage/classification.md`
  at the pinned commit. Only the class criteria are taken; the actor/effect
  cheat-sheet next to them is Airflow-specific and was deliberately not
  copied.
- **Remote path.** Prompt text only. Works with every backend, including the
  agent-cli backend over SSH, with nothing on the remote.
- **Risks.** A backend that ignores the new keys falls back to the old
  derivation; an unknown class is stored as blank, never as an error.

### 4.2 CVE record merge guards (done)

- **What.** The advisory push reads the record the service holds and refuses
  three regressions before the upsert: walking a PUBLIC record back to a
  draft, dropping references somebody added by hand on the service (they are
  unioned in by URL), and renaming the affected product. Each has an override
  checkbox on the push form. Setting PUBLIC without a reference tagged
  `vendor-advisory` is refused with no override, which is the ASF rule that a
  record goes public after the announcement ships.
- **Files.** `security/cve_merge.py` (port with attribution),
  `dashboard/views.py` (`security_cve_advisory_push`), the advisory template.
- **Upstream source.** `tools/cve-tool-vulnogram/oauth-api/src/vulnogram_api/merge_mode.py`
  at the pinned commit (present at 0.1.0, 13 commits since). Ported rather than
  installed: the package's client overlaps `cve_api.py`, and the guards are
  120 lines of stdlib.
- **Remote path.** Web host only; one extra GET per push.
- **Risks.** frank's own `affected[]` carries no `packageName`, so the product
  guard compares `(package, product)` with an empty package on frank's side;
  a record first populated by Vulnogram with a Package URL will differ and
  need the override once. The refusal message says so.
- **Found while fact-checking the plan.** `cve_api` knew only CVE Services'
  RESERVED, PUBLISHED and REJECT. A record in the ASF workflow's own
  `CNA_private` lifecycle (DRAFT, REVIEW, READY, PUBLIC) was stored as
  "unknown", so the panel read "Unrecognised response" right after a push
  these guards had let through to PUBLIC. All four are recognised now, with
  their own labels (migration 0065). The state list is Magpie's
  `tools/cve-tool-vulnogram/record.md`, "State transitions", at the pinned
  commit.

### 4.3 Disclosure scan on fix branches (done)

- **What.** After a fix-agent refresh, every branch on the fork that names the
  finding is scanned for a CVE id, Magpie's security phrases ("sql injection",
  "privilege escalation", ...) plus a few of frank's ("advisory", "vuln"), and
  the report's own CVE id and reporter name. Hits land on
  `SecurityReport.fix_language_hits`, a red badge on the report page, and a
  WARNING. Never a block: frank does not open the PR.
- **Why.** The fix prompt asks the agent in prose to name the branch
  innocuously; the branch list of a public fork is readable before the
  advisory is. Prose is a request, this is the check.
- **Files.** `security/fix_language.py`, `security/fix_agent.py`
  (`refresh_fix_status`), `core/models.py` (migration 0058), the fix-status
  template.
- **Upstream source.** `plugins/magpie-security/skills/issue-fix/guards/security_language.py`
  at the pinned commit. The phrase list is ported; the guard itself is a
  Claude Code hook that needs Magpie's `ctx` shim and `gh`, and so is not
  reusable as code.
- **Remote path.** Web host only (the branch names come from the existing
  `git ls-remote` against the fork). Commit-message scanning would need the
  worker's clone and is a follow-up in `branch_scan`.

### 4.4 Untrusted-content framing in every prompt (done)

- **What.** One rule and one fence for all prompts that carry stranger-written
  text: the PR description, prior comments and diff in both review paths
  (and so every `checks/` sub-check that reuses the user-message builder), the
  report text in parse and triage. `core/untrusted.py` holds the rule
  (adapted from Magpie's `AGENTS.md`) and a nonce-marked fence with
  defusing, generalised from the fix agent's.
- **Why.** The verifier, fix agent and recheck agent already framed their
  input; the review and triage prompts did not, and `docs/security-design.md`
  carried that as open risk H4. Both lenses scored this 4 and 5.
- **Files.** `core/untrusted.py`, `review/prompt.py`, `review/agent_cli.py`,
  `security/prompt.py`; H4 in `docs/security-design.md` records the closure.
- **Remote path.** Prompt text only.
- **Residual.** The full-file and imported-module context read from the PR
  head checkout is contributor-written and is not yet fenced.

### 4.5 Injection scan at every intake door (done)

- **What.** Run the regex injection scanner frank already has
  (`security/malicious_prompt.regex_scan`, used before only by the verifier
  and fix agent) when a report is created by email, paste or zip import, and
  again after triage parses the POC; persist the hits on the report and show
  a persistent callout above the report text. Both lenses scored it 4.
- **Why.** Every later LLM call re-reads the stored report; Magpie persists
  the flag at import for exactly that reason ("so the marker persists for
  every future skill invocation"). frank computed the hits twice, late, and
  kept them nowhere the operator saw before clicking Verify.
- **Where.** `security/intake.py` (`record_injection_hits`, one function the
  doors call), `SecurityReport.injection_hits` (migration 0060), called from
  the email poll in `worker/runner.py`, the paste form in
  `dashboard/views.py`, both create sites in `security/zip_import.py`, and
  `triage._parse_report` after the POC is saved. The callout is in
  `security_detail.html` above the raw text. Recording is the mitigation;
  nothing is refused here, and the verifier keeps its own
  `refuse_on_injection` gate.
- **Upstream source.** `plugins/magpie-security/skills/issue-import/apply.md`
  (callout wording) and `AGENTS.md` (hidden-text list) at the pinned commit.
- **Remote path.** Worker and web processes only.

### 4.6 Forwarder relay detection in the email parser (done)

- **What.** Recognise the ASF Security Team relay (sender pattern, "Dear PMC,
  The security vulnerability report has been received..." preamble, the
  "discovered and reported by" credit line) and GitHub Security Advisory
  relays (`notifications@github.com` with a `(GHSA-xxxx-xxxx-xxxx)` subject),
  and record relay kind and the credited reporter instead of the forwarder's
  address. Both lenses scored it 4.
- **Why.** On an ASF project most reports arrive this way; frank's parser
  only unwraps a generic "forwarded message" block and otherwise credits
  `security@apache.org` as the reporter.
- **Where.** `data_access/email_inbox/parser.py` (`_detect_relay`, the
  `asf-security` adapter's preamble, credit and kind-hint regexes ported
  data for data; a `ghsa` relay of frank's own), `InboxMessage.relay_kind` /
  `reporter_credit` / `credit_kind`, and the same three columns on
  `SecurityReport` (migration 0061) written by all three doors. The detail
  page shows "Relayed via" and "Credited as". `security/cve_advisory.py`
  now emits CVE 5 `credits[]` from `reporter_credit` only, typed `tool` for
  a credited scanner or AI account per Magpie's bot-credit policy, and never
  from `reporter_name` (on a relayed report that is the broker).
- **Two deviations, both upstream-worthy (section 7).** The preamble and the
  credit phrase are matched word-by-word with `\s+` between, because mail
  clients wrap the template at 72 columns and Magpie's literal-space regex
  misses every wrapped copy; and the credit regex runs to the end of the
  line, because Magpie's `(?P<credit>.+?)\.` stops at the first period and
  turns "bugbunny.ai" (its own README example) into "bugbunny".
- **Upstream source.** `tools/forwarder-relay/README.md` (the `asf-security`
  adapter's regexes, present at 0.1.0), `tools/gmail/asf-relay.md`, and
  `tools/cve-tool-vulnogram/bot-credits-policy.md`.
- **Remote path.** Worker and web processes only.

### 4.7 Attachment manifest and the static-first PoC policy (done)

- **What.** Record each email attachment's name, type, size and sha256 at
  intake as data (never extracted, never executed), show them on the report
  page with Magpie's rule text, and keep execution click-gated in the
  container sandbox as it is today. Both lenses scored it 4.
- **Why.** Attachments are dropped silently today, so a report whose PoC is
  a zip reads as having no PoC.
- **Where.** `AttachmentMeta` in `data_access/email_inbox/types.py`,
  `_attachments` in the parser (name, declared type, size, sha256; at most
  50; bytes dropped), `SecurityReport.attachments` (migration 0061), and an
  "Attachments" section on the detail page carrying the rule text. Also fixed
  on the way: a `text/plain` attachment such as `poc.py` was taken as the
  body of an HTML-only mail; `_extract_body` now skips attachment-disposition
  parts, so the PoC lands in the manifest and the real text is the body.
- **Upstream source.** `docs/security/poc-handling-policy.md` at the pinned
  commit (written after 0.1.0).
- **Remote path.** Worker and web processes only.

### 4.8 Security-disclosure scan on public PR text (done)

- **What.** A deterministic `llm_checks` entry, `security-disclosure`, that
  reads a PR's title and description for a CVE id or a phrase that says
  "this is a security fix" and files one `important` draft listing every
  match with the clause it sits in. Both lenses scored it 4. Enabled per
  project like any check; the ASF policy sentence and link go in the body
  when `governance: asf`, a generic rule otherwise.
- **Why.** frank's `security` check reads the code for holes the change
  introduces; nothing read the prose for the opposite leak, a public PR
  announcing a fix before the advisory. ASF policy is explicit that commit
  messages must not reference the security nature of a commit.
- **Where.** `review/checks/security_disclosure.py`, registered in
  `review/checks/__init__.py` and the known-checks list in `config/models.py`.
  The phrase list reuses `security.fix_language.MAGPIE_SECURITY_KEYWORDS`
  plus pr-management's additions; `_context` and the "exploitable over
  exploit" prune are Magpie's. No model call, so a prompt-injected
  description cannot talk it out of a match (Magpie's own test, mirrored).
  Commit messages are not read: the worker clone tracks the default branch
  and the forge clients have no commits fetcher; a gap for later.
- **Upstream source.** `tools/pr-management/src/pr_management/triage/signals.py`
  (`SECURITY_PATTERNS`), `code_review/scans.py` (`security_disclosure`,
  `_context`), `plugins/magpie-pr-management/skills/code-review/classifications/security-disclosure.md`
  (the warning text) at the pinned commit.
- **Remote path.** Worker host only; a regex over two strings.

### 4.9 No live @-mentions in anything frank posts (done)

- **What.** Every `@handle` outside code in a draft body is backtick-quoted
  at post time, so it reads as a handle and notifies nobody; an operator
  allowlist (`mention_allowlist`, Magpie's key name) keeps named handles
  live. The post result says which handles were quoted. Scored 3/4.
- **Why.** A review already notifies the author and every subscriber. A
  handle a model wrote, or quoted from the PR into a finding, summons a
  person nobody decided to summon.
- **Where.** `backends/mentions.py` (`escape_handles`, `mention_scan`),
  applied in `backends/poster._format_comment_body`; `OperatorConfig.mention_allowlist`
  read by the post-review view. One deviation from Magpie: a bare `@word`
  alone on a line is treated as a mention, not a decorator (Magpie's
  `_DECORATOR` let `@bob` on its own line go out live); a decorator here
  needs a dot or a parenthesis.
- **Upstream source.** `tools/pr-management/src/pr_management/code_review/body.py`
  (`escape_handles`, `mention_scan`) and
  `plugins/magpie-pr-management/skills/code-review/posting.md` at the
  pinned commit. The posting rule is three days old upstream (not at
  0.1.0), hence pinned by sha.
- **Remote path.** Web process only, at post time.

### 4.10 Moved-head guard and inline-anchor fold at post time (done)

- **What.** Before posting, the poster reads the PR's live head and refuses
  when it differs from the head the drafts were made against, with two
  overrides rendered on the refusal: post anyway, or post with every inline
  comment folded into the review body. In every mode each inline comment is
  then checked against the current diff and folded when its line is not in
  it. Both lenses scored it 4.
- **Why.** GitHub refuses an inline comment whose line is outside a hunk
  with a 422 for the whole batch, which frank reported as "Failed to post
  review" with nothing posted; and after a push, drafts anchored to the old
  diff land on the wrong lines.
- **Where.** `backends/poster.py` (`StaleHeadError`, `on_stale_head`,
  `_is_anchored` over frank's own `diff_position.translate_line_to_position`,
  `_folded`), the `post_review` view and its override buttons. The head
  the drafts were made against is `PullRequest.head_sha` (set at poll; a
  push marks reviewer runs stale), so no migration. An unreadable head or
  diff posts as before and logs why.
- **Upstream source.** `tools/pr-management/src/pr_management/code_review/cli.py`
  (`_guard`: "new commits since the draft", refresh / post-anyway /
  body-only-now), `diff.py` (`anchor`), `body.py` (`inline_comments`
  folding unanchorable findings) at the pinned commit; the guard exists at
  0.1.0 (`review-flow.md`, "Heads up: PR #N has new commits since I drafted
  this review").
- **Remote path.** Web process only: one PR fetch and one diff fetch at
  post time.

### 4.11 Read-only flags for the claude and codex reviewers (done)

- **What.** A `readonly_args` field on `AgentCLIReviewerConfig`, seeded so the
  `claude` reviewer runs with `--setting-sources user --strict-mcp-config
  --tools Read,Grep,Glob`, `codex exec` with `-s read-only
  --ignore-user-config`, `cursor-agent` with `--mode ask` (moved out of
  its `extra_args`, where an override silently dropped it; a config that
  still restates it there passes it once), and `pi` with `--tools
  read,grep,find,ls` (its default set includes bash, edit and write, and the
  seed passed nothing). Both lenses scored it 4 or 5.
- **Found in review: a denylist leaks.** The first version used Magpie's
  `--disallowedTools Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task`.
  With claude 2.1.299 the session's init event still listed 34 tools,
  `Monitor` (shell commands under the operator's own `Bash(...)` allow
  rules), `Workflow` and `SendMessage` among them. `--tools Read,Grep,Glob`
  leaves those three and nothing else, including tools added later.
- **Found in review: codex's MCP servers.** The first version passed Magpie's
  `-c mcp_servers={}`. codex deep-merges `-c` overrides into the user config,
  so an empty table removes nothing: with codex-cli 0.162.1 and a throwaway
  CODEX_HOME holding one fake server, the override still spawned it, and
  `--ignore-user-config` did not. Auth still comes from CODEX_HOME; a model
  provider or profile in `config.toml` is not loaded either, which is the
  trade, and `readonly_args: ["-s", "read-only"]` restores the config at the
  cost of MCP exposure. Worth reporting to Magpie (section 7).
- **Found in review: the checkout's own settings.** `claude -p` skips the
  workspace-trust prompt, so without `--setting-sources user` it loaded the
  PR's `.claude/settings.json`, and a hook there is a shell command run
  outside the tool system, which `--disallowedTools` does not stop. A
  contributor could run code on the worker by adding that file to a PR.
  Reproduced with claude 2.1.299 (a SessionStart hook in the checkout ran
  under the other flags, not with this one); this predates the read-only
  work, which is where it was caught. `cursor-agent` and `pi` turned out to
  have the same door with no flag to shut it; 4.18 closes it for every agent.
- **Why.** These reviewers run inside a checkout of someone else's PR. With
  a shell, an editor and the operator's MCP servers, an instruction planted
  in the diff has somewhere to go. `cursor-agent` already ran `--mode ask`;
  claude and codex ran with everything.
- **Where.** `config/models.py` (`CLAUDE_READONLY_ARGS`, `CODEX_READONLY_ARGS`,
  the field, `build_invocation`, the seeds and the legacy `claude_cli`
  promotion), `worker/runner.py` (the startup line names the flags and warns
  when an override emptied them). Its own field, like `trust_args`, so an
  `extra_args` override keeps it; `readonly_args: []` is the opt-out. The
  verifier and the `agent-cli` LLM backend borrow reviewer entries through
  `build_invocation`, so they inherit it. An extra entry inherits a seed's
  flags when its argv runs the seed's binary anywhere, by basename, so
  `/home/op/.local/bin/claude`, `uv run claude` and `npx codex` are covered.
  So are npm package specs with or without a version pin:
  `npx -y @anthropic-ai/claude-code@2.1.299`, `@openai/codex@0.162.1` and
  `@mariozechner/pi-coding-agent`. An enabled reviewer whose binary frank
  has no flags for, and which sets none of its own, gets a startup WARNING
  saying it runs with that CLI's default tools.
- **Found in review: the `claude-code` LLM backend.** That backend is the
  documented no-API-key option, and with no triage backend set it also serves
  triage, so it reads contributor diffs and emailed reports. It still ran
  claude with the old denylist, loaded the operator's MCP servers, and ran
  in the shared temp root, where any local user could plant a `.claude/`.
  It now passes `--setting-sources user --strict-mcp-config --tools ""`
  (checked with claude 2.1.299: the init event lists no tools and no MCP
  servers) and runs in a fresh private directory per call.
  The legacy `claude_cli:` block takes `readonly_args` too, for a wrapper
  that rejects unknown flags. Magpie's `--ephemeral` and
  `--skip-git-repo-check` for codex are left out: neither contains anything.
- **Upstream source.** `tools/adversarial-review/src/adversarial_review/backends.py`
  (`CLAUDE_DENIED_TOOLS`, `_codex`, `_claude`) at the pinned commit; not at
  0.1.0.
- **Remote path.** Plain argv; nothing to install on the agent host. A test
  stages the flags through `RemoteSSHExecutor`'s stdin delivery into a real
  shell and checks they come out as the same argv elements.

### 4.12 The pre-push hook gates every spelling of `git push` (done)

- **What.** `.claude/hooks/git_gate.py` decides whether a Bash command is a
  push, a commit or neither by splitting it into simple commands the way a
  shell does, dropping keywords and `NAME=value` prefixes, normalising
  `/usr/bin/git` to `git`, and skipping git's global options before reading
  the subcommand. Heredoc bodies are dropped first, and an untokenisable
  command falls back to a text search that fails closed.
- **Why.** The hook matched `^\s*git\s+push`, so `cd repo && git push`,
  `git -C repo push` and a push inside a retry loop skipped the test run.
  That is how a failing test reached CI on this PR.
- **Found in review.** The first version still missed a push after a `#`
  comment containing an apostrophe, after `)` glued to `;` or `&&`, inside
  `bash -c`, `eval`, `"$(...)"` or backticks, behind `env -i`, `sudo -u`,
  `nice -n` and similar wrappers with options, and after a here-string or a
  quoted `<<` that it mistook for a heredoc. All are handled and tested; a
  literal `'$(git push)'` in single quotes now errs toward running the checks.
- **Upstream source.** `tools/agent-guard/src/agent_guard/__init__.py`
  (`split_segments`, `Segment`, `git_subcommand_index`) at the pinned commit.
- **Remote path.** Developer tooling only; not part of the worker.

### 4.13 Local agent runs in their own process group; a logged-out CLI says so (done)

- **What.** `LocalExecutor.run` starts the agent in its own session with its
  output going to temp files, and kills the whole process group when it
  returns, finished or timed out. Process stdin is a file too, empty when
  there is nothing to send. Separately, a failed agent CLI whose stderr says
  it is not logged in gets one sentence naming where to log it in (this
  machine, or the remote host), at all three agent-CLI error sites: the
  reviewer, the `agent-cli` LLM backend and the security verifier, where it
  also lands in the verdict summary on the report page. Scored 4/3.
- **Why.** With pipes, reading to EOF waited for the last process holding
  them, so a helper the agent spawned could hold a finished review past its
  budget; `subprocess.run` killed only the direct child on timeout, so
  helpers outlived it. And "exited with code 1" from a CLI frank drives with
  its own login, not an API key, is most often "nobody logged it in there",
  which the bare exit code does not say.
- **Where.** `review/tool_executor.py` (`_run_local`, `_kill_group`,
  `looks_like_auth_failure`, `auth_failure_advice`), `review/agent_cli.py`,
  `review/backends/agent_cli_backend.py`, `security/verifier.py`. The local
  runner falls back to plain `subprocess.run` where `os.killpg` is missing.
- **Found in review: orphans at shutdown.** In its own session, an agent no
  longer gets the terminal's Ctrl-C or hangup, and a run on the worker's
  daemon interactive thread is abandoned at exit without its `finally`. A
  Verify press followed by Ctrl-C left the agent running and billing in the
  force-run lane's worktree. `_run_local` now records each group it starts.
  It sends TERM with a 10s grace before KILL on Ctrl-C, as on a timeout. The
  worker's shutdown stops whatever is still recorded
  (`stop_local_children`), and a hangup now runs that shutdown unless
  SIGHUP is ignored (`nohup`).
- **Found in review, second round.** The worker stopped those children only
  after the 30-second join of the interactive thread. A second signal during
  that join, or `make down`'s SIGKILL a few seconds after its TERM, skipped
  the stop, and the stopped run looked like an ordinary CLI failure, so the
  thread went on to start the next one.
  - The shutdown now ignores further signals, stops local children first,
    and then joins.
  - Once stopping, `_run_local` raises `WorkerShutdown`, a KeyboardInterrupt,
    so the command is marked "interrupted by worker shutdown". It starts
    nothing new after that.
  - A Ctrl-C that lands inside any local run sends TERM to every group at
    once.
- **Upstream source.** `tools/adversarial-review/src/adversarial_review/runner.py`
  (`_execute`, `_AUTH_ERRORS`) at the pinned commit; not at 0.1.0.
- **Remote path.** The process-group change is local only: on the remote the
  ssh client is the child, and the agent's own lifetime is the remote
  shell's. The auth advice applies in both modes and names the remote host
  when `remote.mode` is `ssh`.

### 4.14 A failed post is verified, never blindly retried (done)

- **What.** When `create_review` raises, the poster reads the PR's reviews
  back (`list_pull_request_reviews`, added to every forge client by the
  intending-to-merge work) and looks for frank's `MANAGED_MARKER` in a review
  submitted since the attempt, in its body or its comments. Landed: the
  drafts are recorded as posted. Absent: the view says a retry is safe.
  Unknown (the read-back failed too): `PostUnverifiedError`, and the view says
  to check the PR before posting again. Both lenses scored it 3 or 4.
- **Why.** A forge can accept a review and still fail the request: a lost
  response inside the 30-second client timeout, or a 5xx on the follow-up
  comment read. A submitted review cannot be deleted, so "Failed to post
  review" followed by a second click put a duplicate on a contributor's PR.
  The view also used to say "Posted" whatever the poster returned.
- **Where.** `backends/poster.py` (`_find_landed_review`, `PostUnverifiedError`),
  the `post_review` view. Correlation keys never reach the forge, so each
  attempt writes its own `<!-- franktheunicorn-attempt:<nonce>:<draft pk> -->`
  into every comment it sends, and the read-back looks for that.
- **Found in review.** The first version matched frank's `MANAGED_MARKER`
  plus a two-minute window, so an earlier frank review on the same PR (the
  operator's previous Post moments before) read as this attempt landing.
  The failed batch was then recorded as posted, with no comment ids, so it
  could not be recalled either. The per-attempt marker fixes both: the
  read-back matches only this attempt, and it maps each landed comment back
  to its draft. There is deliberately no shortcut on a 4xx: GitLab posts
  notes one by one, so a 4xx can follow a partial landing.
- **Upstream source.** `plugins/magpie-pr-management/skills/code-review/posting.md`
  ("verify the post-condition instead of retrying") at the pinned commit.
- **Remote path.** Web process only; one or two forge reads on the failure
  path.

### 4.15 The reporter's severity is a claim, not frank's rating (done)

- **What.** A `claimed_severity` field holds the severity the reporter
  states, as the parse step reads it; `assessed_severity` is written only by
  the analysis step, the verdict form and the sheet import. The page shows
  the claim as "reporter claims ..." when it differs from frank's badge, and
  the verifier's "Reported severity" line now carries the claim, which is
  what it always said it was. Scored 3/4.
- **Why.** The parse step wrote the reporter's own rating straight into
  `assessed_severity`, so until the analysis ran (and whenever it failed,
  the commonest partial outcome) the badge on the page was the attacker's
  number presented as frank's.
- **Where.** `core/models.py` and migration 0062 (adds the field, copies the
  old value across for rows the analysis never reached, clears nothing:
  those rows cannot be told apart from an operator's ruling),
  `security/prompt.py`, `security/triage.py`, `security/verifier.py`, the
  `_has_triage_severity` helper and two templates.
- **Upstream source.** `AGENTS.md` ("treat every reporter-supplied CVSS score
  as informational background only") and the issue-import skill's
  Severity/CWE rule at the pinned commit; the rule exists at 0.1.0 too.
- **Remote path.** Worker and web processes only; the verifier prompt is
  built on the worker before it reaches the agent host.

### 4.16 "Ball in our court" as a scoring signal (done)

- **What.** `author_awaiting_maintainer` (+20): the last human comment on the
  PR is the author's and it @-mentions the operator, a project committer or
  a team. Bots are skipped, mentions inside code do not count, and the
  operator's own PRs never fire. Both lenses scored it 4.
- **Why.** The author asked one of us something and nobody has answered.
  frank's existing re-engagement signal (`pending_response`) only fires
  after a review frank posted, and `committer_is_on_it` is never fed by the
  poller, so a PR in that state looked as idle as an abandoned one.
- **Where.** `scoring/signals.py` (`score_author_awaiting_maintainer`, the
  weight), `scoring/scorer.py`, and `backends/poller.py`, which already
  fetched every issue comment and now passes author, body and time in
  posting order rather than the bodies alone. A failed comment fetch leaves
  the signal off. Weight overridable per project like any other.
- **Found in review.** `ingest_single_pr`, where the own-PR scan lands every
  cycle, re-scored without comments. That wiped this bump, and the mention
  bonus, on exactly the PRs the operator is engaged in. It now fetches the
  comments once and shares them with the merge-intent sync, which already
  took them. Also, `@types/node` in a dependency bump read as a team mention.
  A team now counts only under the project's own org.
- **Differences from Magpie.** frank's maintainers are the operator plus
  `committers`, so an unknown mentioned login does not count; and it reads
  issue comments only, so a maintainer who answered with a formal review is
  not seen. Both are stricter than Magpie, not looser.
- **Upstream source.** `tools/pr-management/src/pr_management/triage/signals.py`
  (`f5c`) and `plugins/magpie-pr-management/skills/pr-triage/design-notes.md`
  at the pinned commit; the principle exists at 0.1.0.
- **Remote path.** Worker host only.

### 4.17 GitHub's author association decides "new contributor" (done)

- **What.** `PullRequest.author_association` (migration 0063) stores GitHub's
  own answer to "has this author committed here". CONTRIBUTOR, COLLABORATOR,
  MEMBER and OWNER are never new; FIRST_TIME_CONTRIBUTOR and FIRST_TIMER are
  new even when the contributors fetch failed (unless frank already has a PR
  from them); blank (Gitea, GitLab, scrape rows) and NONE fall back to the
  old rule. The routing flag and the score bump use the same helper. Two
  areas proposed it independently; both scored it 4.
- **Why.** frank's known-authors set is the PRs it has ingested plus GitHub's
  contributors listing, which stops at 500 logins. On Spark, a returning
  contributor outside the top 500 with no PR in frank's database read as new
  and went to the new-contributor queue.
- **Where.** `scoring/signals.py` (`association_says_new`, the bump),
  `scoring/moderation.py`, `scoring/scorer.py`, `backends/poller.py` (read off
  the PR payload it already has).
- **Upstream source.** `plugins/magpie-mentoring/skills/welcome/first-time-detection.md`
  and `tools/contributor-metrics` (`MAINTAINER_ASSOC`) at the pinned commit;
  the detection table exists at 0.1.0, in `skills/mentoring-welcome/`.
- **Remote path.** Worker host only.

### 4.18 Agents never start with the checkout's own agent config (done)

- **What.** Every agent run, review or security verification, seeded
  reviewer or the operator's own, goes through `guard_agent_argv`: one
  `sh -c` that moves the checkout's `.claude`, `.cursor`, `.codex`, `.pi`,
  `.gemini`, `.opencode`, `.mcp.json` and `opencode.json(c)` into
  `.git/frank-agent-guard/`, runs the agent, and moves them back. The agent
  never starts with one in place: an entry that will not move ends the run
  with status 97 before the agent starts, and a missing binary is 127
  before anything moves. Whole directories and links, not files inside
  them, so a `.claude -> ~/.claude` symlink hides the link and leaves the
  operator's directory alone.
- **Why.** A PR can carry config that an agent CLI executes on startup, and
  the reviewer runs in that PR's checkout. Read-only flags (4.11) do not
  reach it, because none of it goes through the tool system:
  - `claude -p` runs the hooks in `.claude/settings.json` (reproduced with
    2.1.299). `--setting-sources user` also stops it.
  - cursor-agent's hook loader (build 2026.10.12-d47ca18) reads
    `.cursor/hooks.json` and the Claude-format `.claude/settings.json` and
    `settings.local.json`. `loadProjectHooks` defaults to true and `--trust`,
    which frank must pass, clears the workspace gate. No flag turns it off.
    This was read from the bundle and not executed, because auth is checked
    first. [Cursor's hooks documentation](https://cursor.com/docs/hooks)
    describes project hooks, and [Cursor closed a pre-trust command-execution
    gap](https://thearabianpost.com/cursor-closes-command-execution-gap-before-workspace-trust/)
    in the IDE, which `--trust` sidesteps by design.
  - pi 0.73.1, run here: `.pi/extensions/*.ts` executed at startup with no
    credentials and no prompt, and a package listed in `.pi/settings.json`
    was `git clone`d even under `--no-extensions`. An `npm:` package would
    have run its install scripts.
  - codex loads `.codex/config.toml` and project hooks only for a project its
    config marks trusted, which frank never does (read from the codex-rs
    config loader). It is listed anyway, so an operator's own trust entry
    cannot switch it on behind frank's back.
- **What comes back.** Restoring is a move back, not a `git checkout`, so the
  tree afterwards is exactly what it was. That includes the PR's uncommitted
  state and anything untracked the operator keeps in the clone: the first
  draft deleted files with `rm`, which would have lost a hand-written
  `.claude/settings.local.json` for good. The restore waits for the agent to
  exit: the guard runs it in the background and traps HUP, INT and TERM,
  and `_run_local` now sends TERM with a 10s grace before its SIGKILL. A run
  killed outright leaves the entries in the stash, and the next guarded run
  puts them back before anything else. When the tree has the entry again in
  the meantime (the next PR's checkout wrote it), the tree's copy wins and
  the stashed one moves to `.git/frank-agent-guard.displaced/`, never
  deleted, with a WARNING naming where. A killed run does not block the next
  checkout, because git treats a missing tracked file as up to date when it
  switches commits; a test pins that.
- **Found in review: the stash is tied to its commit.** The first version
  put a stash back into whatever was checked out, which planted one PR's
  config in another PR's tree in two cases:
  - after a killed run;
  - remotely, when a run that timed out locally kept going on the host while
    the runner checked out the next PR.

  The planted files then refused every later checkout of a commit that
  tracks those paths. The stash now records the commit it came from and is
  put back only into that commit. Anywhere else it moves aside, and git
  restores the current commit's own tracked copy, so the tree stays clean
  whichever way HEAD moved. Separately, a lock whose pid could not be
  written is removed instead of being left empty forever. A lock directory beside the stash,
  holding the run's pid, refuses a second run in the same checkout while the
  first is alive. The worker's lanes already prevent that, but if two ever
  met, the second's recovery would hand the first agent its hooks back. A
  lock whose pid is gone is taken over.
- **Where.** `review/tool_executor.py` (`AGENT_CONFIG_ENTRIES`,
  `guard_agent_argv`, `unwrap_guarded`, `guard_notes`, the TERM grace),
  `review/agent_cli.py` and `security/verifier.py` (the two agent call
  sites). The `agent-cli` LLM backend is not wrapped: it runs in an empty
  scratch directory, not a checkout.
- **Upstream source.** None. Magpie's adversarial-review runner starts its
  agents with the repository under review as their working directory
  (`runner.py`, `ctx.repo_dir`, at the pinned commit) and nothing moved
  aside, so the same finding applies to it (section 7).
- **Remote path.** One argv and no stdin, so it fits both delivery shapes.
  The agent's stdin is `/dev/null`, so it cannot eat the rest of a
  stdin-delivered script. The host needs a POSIX `sh`, `mv` and `git`, all
  already assumed. Tests run the real script under dash and `bash --posix`,
  through stdin delivery and through argv delivery.
- **Not covered.** Prompt material the CLIs read from the checkout
  (`AGENTS.md`, `CLAUDE.md`, and `.cursor/rules` outside a guarded run) can
  still steer a review. That is an integrity problem, not code execution,
  and the untrusted-content framing (4.4) is the defence there.

### 4.19 An approver can take a yes back (done)

- **What.** Intending-to-merge clears when the approver's latest word takes
  the approval back. That is a leading `-1`, a first-person retraction
  ("retracting my LGTM", "I take back my +1") or an imperative hold about this
  PR ("please hold off merging", "do not merge yet", "this is not ready to
  merge"). CHANGES_REQUESTED already cleared it, the way it always has. A
  DISMISSED review is skipped. GitHub does not say whether it was an approval
  or a changes request, and dismissing a changes request is how a maintainer
  unblocks a PR. GitLab approvals keep their `approved_at`, so a withdrawal and
  an approval sort in the order they happened.
- **Why.** Approved and then "-1, this breaks the shuffle path" stayed in the
  queue, one click from `merge_pr` where the merge queue is on. A dismissed
  review whose body said "LGTM" counted as an approval while `count_approvals`
  read zero.
- **Found in review.** The first version had several gaps.
  - It read "don't merge these two maps" and "hold off merging #123" as holds.
  - A gated retraction ("I take back my LGTM if the tests don't pass") read
    as a fresh conditional approval.
  - It let a dismissed changes request clear an approval.
  - GitLab approvals sorted first, so one old "please hold off" beat a
    current approval for good.

  Holds now need this PR as their object. Retractions are checked before
  approvals. A `-1` that the same comment answers with "+1" or "LGTM" is a
  scoped objection, not a veto. Approvals are still checked before holds, so
  "LGTM, but don't merge until the branch is cut" stays an approval.
- **Where.** `worker/merge_intent.py` (`classify_body`, `detect_merge_intent`,
  `INTENT_WITHDRAWN`), `backends/gitlab.py` (`list_pull_request_reviews`),
  CLAUDE.md "Intending to merge is a queue".
- **Upstream source.** `plugins/magpie-release-management/skills/vote-tally/SKILL.md`
  (Step 1, and "One person, one vote": the latest vote counts, `-1` is a no, a
  retraction is not a vote) at the pinned commit; the retracted-is-ambiguous
  rule is also at 0.1.0 in `skills/release-vote-tally/SKILL.md`. The regexes
  are frank's own.
- **Remote path.** Worker host only, over the reviews and comments the poller
  already fetched.

### 4.20 Only a final release tag means "released" (done)

- **What.** `fix_landed` answers `released`, which is terminal, only when a
  final release tag contains the fix. Any other tag still proves the commit is
  in published history, so it is `merged` and stays open to the next sweep,
  with a note naming the tags. That covers a candidate (`v3.5.0-rc1`,
  `4.0.0-preview1`) and a scheme the project's rule does not recognise.
  `introduced.py` lists releases by the same rule.
- **Why.** Any tag answered `released`, so a fix that so far reached only a
  release candidate was stamped released for good, and on a security report
  that reads as "the advisory can go out".
- **The rule.** `security/release_tags.py`. A final release is `[v]X.Y[.Z…]`,
  optionally `.postN`, and (frank's addition for ASF podlings) `-incubating`.
  Anything else that starts version-shaped is a pre-release. A namespaced tag
  counts only under `ProjectConfig.release_tag_prefix` (`release-` for Flink,
  `rel/` for Hadoop). For schemes no prefix expresses (`STRUTS_7_4_0`,
  `REL_5_2_5`), `release_tag_pattern` is a regex a final tag must match in
  full, validated at config load.
- **Found in review.** The first version had three gaps.
  - It wrote `not-merged` for a commit in a tag the default rule did not
    recognise, which the tag itself disproves.
  - It had no answer for `-incubating` or underscore schemes.
  - Its migration emptied candidate-only `released` rows, which would have let
    an indeterminate non-answer replace a git proof.

  Migration 0064 now downgrades those rows to `merged` and keeps the tags as
  `other_tags`. The next sweep re-reads them under the project's rule.
- **Where.** `security/release_tags.py`, `security/fix_landed.py`
  (`_ancestry_verdict`, `_test_ref`), `security/introduced.py`,
  `config/models.py` (`release_tag_prefix`, `release_tag_pattern`), migration
  0064, CLAUDE.md's fix_landed paragraph.
- **Upstream source.** `plugins/magpie-release-management/skills/prepare/scripts/prev_tag.py`
  (`FINAL_RE` and the `--tag-prefix` handling) at the pinned commit; not at
  0.1.0.
- **Remote path.** No new invocation. The existing `git tag --contains` runs
  through the verifier reviewer's executor (on the agent host in ssh mode),
  and only the worker's reading of its output changed.

---

## 5. Tier 2: Adopt Later

Every survivor that is not on this branch and is code rather than text, ranked
by value and then effort. The ones to do next are marked "(next)". Each port
names its upstream file and commit in a docstring and adds its module to
ATTRIBUTION.md's Magpie row, the same way the section 4 ports do. "Pin" says
what, if anything, enters `src/franktheunicorn/magpie/magpie-pin.yaml`. For
tests that touch the agent host, the pattern is the one in
`tests/review/test_tool_executor.py`. Patch
`franktheunicorn.review.tool_executor.subprocess.run` and build
`RemoteSSHExecutor(config=_ssh_config())`. Then assert on the remote command
string, `mock_run.call_args.args[0][-1]`, or run the staged script through a real
`sh` as `TestRemoteCommandDelivery` and `TestStdinFramingUnderAPty` do.

### 5.1 An approver's withdrawal clears intending-to-merge (done, 4.19)

Done on this branch; see 4.19. The number is kept so the references below
stay valid.

### 5.2 Only a final release tag means "released" (done, 4.20)

Done on this branch; see 4.20.

### 5.3 Disclosure scan over fix-branch commit messages (next)

- **What.** Extend 4.3 from fork branch names to the commit messages on each
  fork branch that names a finding.
- **Why.** An agent that names its branch innocuously but writes "fix CVE-2026-1234
  RCE" in the commit message has published the hole on a public fork before the
  advisory. 4.3 deliberately left this as the follow-up. Both lenses scored the
  guard 4.
- **Files.** frank: `security/branch_scan.py` (`match_fix_branches` at 657 already
  reads commit messages per branch through `gather_branch_evidence`'s
  `executor.run` at 440, but only for branches on `origin`, the upstream, and
  only for reports whose `fixed_in_branch` is empty; `score_branch`'s docstring
  at 555-557 says the fix agent's branches reach `origin` only once pushed
  upstream);
  `security/fix_language.py` (`scan` at 74 already takes any text;
  `scan_branches` at 100 wraps it for names);
  `security/fix_agent.py` (`fork_full_name` at 161, `_disclosure_terms` at 705);
  `SecurityReport.fix_language_hits`; `tests/security/test_branch_scan.py`,
  `tests/security/test_fix_language.py`. Magpie:
  `plugins/magpie-security/skills/issue-fix/guards/security_language.py:34-56`
  (the same lines are at tag 0.1.0 in
  `skills/security-issue-fix/guards/security_language.py`).
- **Pin.** None; the phrase list is already ported in `fix_language.py`.
- **Remote path:** two argv calls through the scan checkout's executor, no stdin:
  `git fetch --no-tags <fork_url> +refs/heads/<branch>:refs/frank-fork/<branch>`,
  then `git log -n 200 --format=%B origin/<base>..refs/frank-fork/<branch>`.
  Output is bounded by `-n`, and on a remote driven over stdin also by the 4 MB
  cap (`_MAX_REMOTE_OUTPUT_BYTES`). The agent host needs only `git` and
  network to the git host, both already assumed.
- **Steps.**
  1. Beside `match_fix_branches`'s candidate loop, not inside it (its candidates
     are only reports with an empty `fixed_in_branch`, and it returns before the
     fetch when there are none), for each report with a fork branch, fetch it
     into a frank-private ref (the scan cannot read it as `origin/<branch>`) and
     read its messages.
  2. Scan the messages with the same terms as the branch names, and append the
     hits to `fix_language_hits` with "commit message" as the location. Today
     `refresh_fix_status` reassigns that field from branch names alone
     (`fix_agent.py:664`), so the refresh must keep the commit-message entries or
     the next refresh erases them.
  3. Queue the same scan after a refresh that saw a new branch sha, through
     `security/queue.py` at bulk priority.
  4. A failed fetch or log logs WARNING naming the host and
     `remote_workspace_dir`, and shows no badge rather than a false "clean".
- **Tests.** Unit: a message with a CVE id yields a hit; a clean message yields
  none. ssh mode: two patched `subprocess.run` results; assert the remote
  commands are the fetch into `refs/frank-fork/` and the bounded `git log`, both
  as single argv lines after `cd`.
- **Risks.** The keyword list is deliberately narrow (Magpie's own comment), so
  a clean scan is not proof. One extra fetch per fork branch per sweep.

### 5.4 Show prior-fix evidence beside the Fix button (next)

- **What.** A pure `prior_fix_evidence(report)` that reads only row state,
  rendered as an "Already fixed?" list above the Fix button. The button's
  confirm text carries the count and the strongest kind. The list is recorded on
  launch. It never refuses.
- **Why.** `_gate_reason` (`security/fix_agent.py:187-207`) checks only
  enabled/key, already launched, patch, bug id, project and fork. A launch is a
  paid Cursor run. Signals that the fix already exists are on the row, and
  mostly elsewhere on the page, but not next to the button:
  - `has_confirmed_branch` (`core/models.py:752`);
  - `branch_match_branch` (1211);
  - `fix_branch_primary` and `fix_merged_upstream` (1097, 1113);
  - `recheck_status` (1186);
  - `fix_landed_status` (1136);
  - `duplicate_of` (957);
  - the FIX-ALREADY-PUBLIC disposition (674);
  - `NO_FIX_OWED_STATUSES` (720);
  - `cve_record_state` (942);
  - PR links in `operator_notes` and `external_notes` (971, 984).

  Magpie's issue-fix makes this check "mandatory before any new code is written".
- **Files.** frank: `security/fix_agent.py`, `dashboard/views.py`
  (`security_report_detail` at 2277, `security_report_fix` at 2890, the refresh
  view), `dashboard/templates/dashboard/_security_fix_status.html` (the
  `hx-confirm` at 64), `security_detail.html`, `core/models.py`
  (`fix_launch_evidence` JSONField, a migration), `tests/security/test_fix_agent.py`.
  Magpie: `plugins/magpie-security/skills/issue-fix/pre-implementation.md:6-9, 55`
  (sha256 `c93764ede3b3857fe27886dfe802a5787b76749545dc0b8e169456056c5a4881`).
- **Pin.** None (port of a principle). Docstring cites the file above at
  dadb315caa8b881fc1d2195bef03223dac25915a; the same Step 2 text is at tag 0.1.0
  in `skills/security-issue-fix/SKILL.md` (about lines 305-360).
- **Remote path:** dashboard request only; the Cursor cloud agent is HTTPS and
  never touches the SSH executor.
- **Steps.**
  1. A `FixEvidence` frozen dataclass (kind, text, strength: proof, pointer or
     context, optional url) and the helper over the fields above. A `git`
     recheck and a merged or released `fix_landed` are proof; a machine branch
     tie and an agent recheck are pointers; an operator status and a published
     CVE are context.
  2. PR links: only `github.com/<project>/pull/N` URLs, and `#N` immediately
     after "PR".
  3. One context helper feeds all three renderers of the fix panel.
  4. `launch_fix_agent` stores the snapshot in `fix_launch_evidence` and logs
     INFO "launched over N prior-fix signal(s)".
- **Tests.** Each rule in isolation; the confirm text names the strongest kind;
  a launch over evidence stores it and still launches.
- **Risks.** Signals of different strength in one list can read as a verdict;
  label each and say it is evidence, not a ruling, as the duplicate note on the
  page already does ("a suggestion from text comparison, not a verdict").

### 5.5 Magpie's CVE 5 builders behind `build_cve5_json`

- **What.** When the optional extra is installed, `cve_advisory.build_cve5_json`
  uses Magpie's argument-only builders. `parse_cwe` gives CWE in long form.
  `parse_affected_versions` gives strict version ranges; its `ValueError`
  becomes a preview warning. `classify_reference` tags `patch` and
  `vendor-advisory`. `build_problem_types` and `build_descriptions` are taken
  too. New fields: `SecurityReport.cwe`, `public_summary`, `advisory_url`.
  frank keeps its own envelope and its own `_credits`.
- **Why.** frank's record has no CWE, carries the operator's version sentence as
  a single version (`security/cve_advisory.py`, `_affected` at 112), and
  emits untagged references (`_references` at 128). The 4.2 merge guard refuses
  any PUBLIC push without a `vendor-advisory` tag, which frank never emits, so
  today the operator adds that reference by hand, in the preview JSON or on the
  service, whose references the guard unions in by URL. Magpie's builders are
  modelled on a real record exported from Vulnogram (`cve_json.py:26-28`) and were
  written for Airflow's tracker (`cve_json.py:17`).
- **Files.** frank: `security/cve_advisory.py`, `core/models.py` (three fields, a
  migration), `dashboard/templates/dashboard/_security_cve_advisory.html`,
  `pyproject.toml` (the extra and a mypy override), `magpie/magpie-pin.yaml`,
  `tests/security/test_cve_advisory.py`. Magpie:
  `tools/cve-tool-vulnogram/generate-cve-json/src/generate_cve_json/cve_json.py`
  (`parse_cwe` 631, `parse_affected_versions` 673, `classify_reference` 859,
  `build_descriptions` 1212, `build_problem_types` 1230).
- **Pin.** Add a `tools:` entry
  `{name: generate-cve-json, subdirectory: tools/cve-tool-vulnogram/generate-cve-json, console_script: generate-cve-json}`,
  and in `pyproject.toml`
  `[project.optional-dependencies] magpie = ["generate-cve-json @ git+https://github.com/apache/magpie.git@dadb315caa8b881fc1d2195bef03223dac25915a#subdirectory=tools/cve-tool-vulnogram/generate-cve-json"]`.
  That is the string `PinnedTool.requirement()` renders; a test asserts they
  agree. Add `[[tool.mypy.overrides]] module = "generate_cve_json.*"`
  `ignore_missing_imports = true`, because the package has no `py.typed`. The
  package is at tag 0.1.0 but 13 commits behind, so pin the sha. See question 4
  on the generic distribution name.
- **Remote path:** web host only (a `pip` install into frank's venv); nothing on
  the agent host.
- **Steps.**
  1. Import lazily inside the function. The module reads a cwd-relative
     `.apache-magpie-overrides/` config at import (`cve_json.py:137, 441-442`).
  2. Call only the five builders above. They take everything as arguments.
  3. Do not call `build_references`: without a config file its
     `TRACKER_FILTER_TOKEN` is empty and it drops every URL (`cve_json.py:333, 965`).
  4. Do not call `build_affected` (a non-empty value reaches
     `_populate_constants()` at 1102-1103, which needs the config file),
     `wrap_cve_record` (it sets the workflow state from body-field readiness,
     which in frank is the operator's call), `_set_config_path` (202; a partial TOML
     raises `KeyError` in `_populate_constants`, 226-243) or `fetch_issue`
     (shells to `gh`).
  5. The two lenses disagreed on whether `_set_config_path` is mandatory. It is
     only if `build_references` or `build_affected` is called, and steps 3 and 4
     avoid both.
  6. The operator's `advisory_url` (the public announce or security.apache.org
     page) gets the `vendor-advisory` tag.
- **Tests.** With and without the extra (monkeypatched import). CWE comes out
  in long form. The advisory URL is tagged, and 4.2's guard then accepts a
  PUBLIC push. A bad version sentence shows a warning, not a 500.
- **Risks.** Three new fields the operator must fill, in one migration. The first
  git dependency in an optional group (`pyproject.toml` has none today). The builders' API carries no stability
  promise, so every call sits in one function and a bump touches one place.

### 5.6 Agent CLIs start with a minimal environment

- **What.** Every agent-CLI run starts under `env -i` with a fixed base list
  plus a per-reviewer `env_passthrough`. The base list is `HOME`, `PATH`,
  `SHELL`, `TERM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `USER`, `LOGNAME`, `TMPDIR`,
  the four `XDG_*` directories, `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` and
  `SSL_CERT_FILE`. Names in the passthrough are validated as Magpie validates
  `AGENT_ISO_ALLOW`.
- **Why.** `_run_local` spawns the agent with `subprocess.Popen(...,
  start_new_session=True)` and no `env` (`review/tool_executor.py`), so a
  local reviewer inherits `FRANK_GITHUB_TOKEN`, `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `REVIEW_AGENT_SMTP_PASS` and
  `CURSOR_API_KEY` from `compose.yaml`. Test containers already get an explicit
  environment (`worker/test_runner.py`, security-design H1). 4.11 took the shell
  away from the seeded claude and pi reviewers and put codex in its read-only
  sandbox, which narrows the exposure. A reviewer for a binary frank does not
  seed, or `readonly_args: []`, reopens it. In ssh mode the
  worker's environment does not reach the remote (ssh forwards none of it
  unless the operator configured `SendEnv`), so the strip there governs only
  what the remote shell's own startup files export. The value is highest in
  local mode.
- **Files.** frank: `review/tool_executor.py` (`_run_local`,
  `RemoteSSHExecutor.run`, `run_script`, `guard_agent_argv`),
  `config/models.py` (`AgentCLIReviewerConfig.env_passthrough`),
  `worker/runner.py` (the startup line), `review/agent_cli.py`,
  `review/backends/agent_cli_backend.py`, `security/verifier.py`,
  `docs/security-design.md` (H1 extended to agents).
  Magpie: `tools/agent-isolation/agent-iso.sh:109-132` (the list), `:164` (the
  name regex), `:297` (`exec env -i`).
- **Pin.** None; port the list and the regex with a comment citing the path at
  dadb315caa8b881fc1d2195bef03223dac25915a. The script exists at 0.1.0 with
  the same list and the generic `agent-iso <cli>` entry, but it reads only
  `CLAUDE_ISO_ALLOW` and does not validate the names. Do
  not vendor the script: `magpie_sync`'s provenance header is an HTML comment
  (`pin.provenance_header`), which a shell script cannot carry.
- **Remote path:** script text through `run_script`: `cd <cwd>`, build the kept
  list with `set --` so unset variables are omitted and values with spaces
  survive, then `env -i "$@" <guarded argv>`. It needs POSIX `env` and `sh`;
  `HOME` passes, so `~/.claude`, `~/.codex` and `~/.cursor` logins keep working.
- **Steps.**
  1. Add the field and the base list. The strip goes outside the 4.18 guard,
     so the guard's `mv` and `git` get `PATH`.
  2. Local: pass `env=` to `Popen` for agent runs only. `_run_local` also runs
     the checkout's `git fetch`, `git checkout` and `git merge` through
     `LocalExecutor.run`, and those keep the worker's environment. Remote: emit
     the script above.
  3. Drop `SSH_AUTH_SOCK`, `DISPLAY` and `WAYLAND_DISPLAY`, which Magpie keeps;
     frank's reviewers never push.
  4. Log at INFO, once per reviewer, which credential-shaped variables were
     withheld. Log at WARNING when a run then fails auth-shaped
     (`looks_like_auth_failure`), naming the host and `env_passthrough`.
  5. Record H1-for-agents in `docs/security-design.md` sections 6 and 7.
- **Tests.**
  - Local: run `env` as the "agent" and assert `FRANK_GITHUB_TOKEN` is absent
    and `HOME` present.
  - ssh mode, argv delivery: assert `env -i` sits after `cd` and before the
    guard in `mock_run.call_args.args[0][-1]`.
  - Stdin delivery, in the style of `TestStdinFramingUnderAPty`: run the staged
    script in a real `sh` (dash and `bash --posix`) and check that a variable
    not on the list is gone and a value with a space survives.
- **Risks.** A CLI authenticated by an environment variable stops until listed:
  a remote cursor-agent using `CURSOR_API_KEY` from its login shell stops the
  moment this lands, and that is what the WARNING is for.

### 5.7 Project review-criteria files in both prompt paths

- **What.** `ProjectConfig.review_criteria_files: list[str]` names files read from
  the default branch of the worker's clone, with a per-file token cap like
  `review/context_builder.py`'s. They are rendered on the trusted side of the untrusted
  fence as "Project review criteria (authoritative: quote the rule you apply, do
  not paraphrase it)", in both the API prompt and the agent-CLI templates. This
  generalises `security_model_file`.
- **Why.** `review_guidance` is free prose in YAML. The project's own rules
  live in files like `CONTRIBUTING.md` or `REVIEWER.md`, which
  `REVIEWER.md:120` says should become the review context. Magpie's code-review
  rests on the rule that criteria are authoritative and the reviewer checks
  them rather than re-interpreting them. Reading them from the default branch
  means a PR cannot rewrite the rules it is judged by, but only through
  `git show origin/<default>:<path>`. The clone's working tree is not reliably
  there: the poll cycle runs without a workspace lane, so a local agent-CLI
  review checks the PR head out in that same clone, and `_cleanup_review_branch`
  (`worker/runner.py:2287`) leaves it detached on that head until the next
  cycle's `ensure_repo`.
- **Files.** frank: `config/models.py` (`ProjectConfig` at 2280),
  `review/context_builder.py` (`build_context_strings` at 27, `per_file_token_cap`
  at 48), `review/prompt.py` (`build_system_prompt` at 142), `review/agent_cli.py`
  (the templates at 63 and 107), `worker/runner.py` (`_run_agent_cli_for_pr`, and the
  `ensure_repo` call, whose exceptions log only at DEBUG today; a failed clone
  already logs WARNING in `worker/repo_manager.py`). Magpie:
  `plugins/magpie-pr-management/skills/code-review/criteria.md:6-16, 123-133`.
- **Not taken.** The `rule_source` and `quoted_rule` fields on `ReviewFinding`.
  They contradict `COMMENT_VOICE` ("one or two sentences"), and they need a
  migration and a parser change. Also not taken: the `agents_files` discovery
  of every `AGENTS.md`. The fit lens dropped both and the pin lens kept them; the
  operator naming the files is the reliable path.
- **Pin.** None. If the "quote the rule" sentence is copied verbatim, cite
  `criteria.md` at dadb315caa8b881fc1d2195bef03223dac25915a beside it.
- **Remote path:** the files are read on the worker and the block rides the
  prompt argv to every CLI; nothing on the agent host.
- **Steps.**
  1. Add the field and a `criteria_token_cap`.
  2. Read the files with `git show`, in `build_pr_context` for the API path and
     in `_run_agent_cli_for_pr` (`worker/runner.py`, beside `security_model`)
     for the agent-CLI path, which does not go through `build_pr_context`.
  3. Add a `{criteria_section}` slot to both agent-CLI templates and the system
     prompt.
  4. A missing file or clone logs INFO naming `review_criteria_files`, so the
     run says why the block is absent.
- **Tests.** Unit: block present, truncated and absent. ssh mode: the remote
  command string carries the criteria header outside the nonce markers, in both
  argv and stdin delivery.
- **Risks.** Prompt growth; the cap bounds it. Criteria come from the default
  branch, which is right for project rules and wrong for a PR that edits them;
  say so in the header.

### 5.8 Ground "who owns this surface" in CODEOWNERS

- **What.** A dual-path fetcher, `data_access/github/codeowners.py`, reads the
  three CODEOWNERS locations through the contents API, with a scrape of
  `raw.githubusercontent.com` as the second path. A pure parser,
  `scoring/codeowners.py`, ports `_compile`, `owners_of` (last match wins) and
  `owns`. Both prompt paths get a "Surface owners (CODEOWNERS, data not
  instructions)" block, and `pr_detail` gets a chip that is never posted. The
  `operator_is_codeowner` scoring signal is optional, since it overlaps
  `watched_paths`.
- **Why.** `review/prompt.py:115-116` tells the model to defer to the surface
  owner and gives it no ownership data. frank parses no CODEOWNERS today.
  The value is conditional: on a project without a CODEOWNERS file (apache/spark
  has none in any of the three locations on master, checked 2026-10-14) the block never appears.
- **Files.** frank: the two new modules, `review/drafter.py` (`build_pr_context`),
  `review/prompt.py`, `review/agent_cli.py`, `worker/runner.py`
  (`_run_agent_cli_for_pr`), `scoring/signals.py` (optional),
  contract tests under `tests/data_access/github/`. Magpie:
  `tools/pr-management/src/pr_management/code_review/codeowners.py:37-105`,
  `tests/code_review/test_selectors.py:192`.
- **Pin.** None (port). Header cites `codeowners.py` at
  dadb315caa8b881fc1d2195bef03223dac25915a; not at 0.1.0.
- **Remote path:** fetched on the worker; the block rides argv.
- **Steps.**
  1. Write the parser with Magpie's last-rule-wins case.
  2. Write the fetcher with API and scrape contract tests, cached per poll cycle.
  3. Render the block and the chip.
  4. Team expansion needs `read:org`. Without it, log INFO once and match direct
     handles only.
- **Tests.** Glob cases (`**/`, `*` not crossing `/`, bare names at any depth,
  trailing `/`). API and scrape give the same rules. ssh mode as 5.7.
- **Risks.** Team rosters need a scope frank's token may lack; inert on projects
  without the file.

### 5.9 Paginate GitLab and Gitea open-PR listings

- **What.** GitLab follows `X-Next-Page` with `per_page=100`. Gitea pages until
  an empty page, or follows `Link: rel="next"`. Both cap at
  `MAX_LISTED_PULL_REQUESTS` and set `lists_all_open_pull_requests = True`, so
  the poller's closed-PR reap runs for them.
- **Why.** Both clients read one page of 50 (`backends/gitlab.py:71`,
  `backends/gitea.py:65`). `base.py:67-73` says to flip the flag once they
  paginate, and `poller.py:789` skips the reap until then, so closed MRs stay
  open in frank's queues. The value is medium only because no known
  deployment of frank uses GitLab or Gitea today.
- **Files.** frank: `backends/gitlab.py`, `backends/gitea.py`, `backends/base.py:73`,
  `backends/poller.py` (`_close_missing_pull_requests` at 775, the cap at 805),
  `tests/test_gitlab_client.py`, `tests/test_gitea_client.py`. Magpie:
  `tools/gitlab/src/magpie_gitlab/client.py:196-257`,
  `tools/gitlab/tests/test_client.py:313-375`, `tools/forgejo/operations.md:61-75`.
- **Pin.** None (reimplemented on httpx). Cite both files at
  dadb315caa8b881fc1d2195bef03223dac25915a; neither exists at 0.1.0.
- **Remote path:** worker host only.
- **Steps.**
  1. Write a `_get_paged` helper per client.
  2. Gitea clamps `limit` to its `MAX_RESPONSE_ITEMS` (default 50). A page
     shorter than requested is therefore not the end; stop on an empty page or a
     missing `rel="next"`.
  3. Any page failure raises, so the reap never runs on a partial listing.
  4. Set the flag on both clients.
- **Tests.** Multi-page and clamped-page cases with `pytest-httpx`. The poller
  reaps a missing MR on a GitLab project and skips the reap when a page fails.
- **Risks.** Turning the flag on makes the reap trust the listing; the
  `MAX_LISTED_PULL_REQUESTS` guard must still apply.

### 5.10 Release-hygiene scans from `tools/pr-management`

- **What.** A deterministic `llm_checks` entry, `release-hygiene`
  (`review/checks/release_hygiene.py`, a `scan()` like `security_disclosure`). It
  calls Magpie's `compiled_artifacts`, `third_party_licences` and
  `licence_headers` over the PR diff, and `ai_disclosure` over the PR body and
  template (the disclosure nit 5.13 also raises; only one of them should). Severities map blocking to critical,
  major to important and minor to nit. Findings are file-level and fold into the
  review body. It runs only when `governance: asf`.
- **Why.** frank has no licence, compiled-artifact or header checks. The four
  scans are most of `scans.py`'s 495 lines (the rest is mainly the
  security-disclosure scan frank has from 4.8, `images` and `agents_files`) and
  24 of the 31 cases in `test_scans.py`, and on an ASF project they are release
  blockers. `licence_headers` is ASF-only by construction (`_APACHE_HEADER` at
  `scans.py:354`), hence the gate.
- **Files.** frank: the new check, `review/checks/__init__.py` (`_get_registry` at
  51), the known-checks list and a `release_hygiene.header_tool_in_ci` flag in
  `config/models.py`, `core/models.py` (`ReviewDraft.CATEGORY_CHOICES`, a
  migration), `review/drafter.py` (`_derive_category`), `pyproject.toml`.
  Magpie: `tools/pr-management/src/pr_management/code_review/scans.py:114-131, 164-181, 204-294, 420-477`,
  `code_review/diff.py`, `tests/code_review/test_scans.py`.
- **Pin.** Add a `tools:` entry
  `{name: pr-management, subdirectory: tools/pr-management, console_script: pr-management}`
  and add
  `pr-management @ git+https://github.com/apache/magpie.git@dadb315caa8b881fc1d2195bef03223dac25915a#subdirectory=tools/pr-management`
  to the `magpie` extra. Add the mypy override `pr_management.*`. The package
  was created on 2026-10-10 (`f8ac4d73`) and its `code_review` module the same
  day (`a7189e1b`); neither is at 0.1.0. All imports sit in
  the one adapter module, so a bump touches one file. See question 4: the
  fallback is a port.
- **Remote path:** worker host only, over the forge-API diff.
- **Steps.**
  1. Write the adapter.
  2. `licence_headers` defers to a header tool by matching CI check names, and
     frank stores none. Pass `["apache-rat"]` when `header_tool_in_ci` is true.
  3. When the extra is missing, log WARNING once naming
     `pip install franktheunicorn[magpie]` and skip.
  4. On a non-ASF project, log INFO naming `governance` and skip.
- **Tests.** Port the 24 `test_scans.py` cases for the four scans against the
  adapter. Extra
  absent and non-ASF gating are tested too.
- **Risks.** An internal API first committed on 2026-10-10; the generic distribution name.
  Adopting it for non-ASF projects would flag MIT files.

### 5.11 Check the public CVE record on cve.org

- **What.** `cve_lookup.fetch_cve_org_state(cve_id)` reads
  `https://cveawg.mitre.org/api/cve/<id>` without auth for `cveMetadata.state`
  and `datePublished`. It is stored as `public_record_state` and
  `public_record_published_at`. The CVE panel warns "pushed PUBLIC in Vulnogram,
  but cve.org has no datePublished".
- **Why.** `cve_record_state` comes only from the token-gated cveprocess service
  (the allocate response, and `cve_api.fetch_record` at 272), and Magpie's record notes warn that writing
  PUBLIC is not the same as submitting to CVE Services. Three state vocabularies
  are in play (frank's `cve_record_state`, Vulnogram's `CNA_private.state` on
  cveprocess, cve.org's `cveMetadata.state`), so the result gets its own
  fields. Do not feed the `fix_landed_*` columns: they say whether the fix is in the
  code, from git or the cloud agent.
- **Files.** frank: `security/cve_lookup.py` (next to `search_cves` at 43),
  `core/models.py` (two fields), `dashboard/views.py` (`security_report_cve_check`
  at 3563), the CVE template, `tests/security/test_cve_lookup.py`. Magpie:
  `tools/cve-org/tool.md:50-94`, `tools/cve-tool-vulnogram/record.md:177-214`.
- **Pin.** None. `tools/cve-org/tool.md` is present at 0.1.0 (with a plain `curl`
  recipe); the record.md section on PUBLIC landed after it (e6553702,
  2026-09-08). Cite both at
  dadb315caa8b881fc1d2195bef03223dac25915a.
- **Remote path:** web or worker host; an outbound HTTPS call of the same class
  as the existing NVD lookup.
- **Steps.** `cve_api._record_state` already knows Vulnogram's DRAFT, REVIEW,
  READY and PUBLIC (fixed on this branch; see 4.2), so the panel can tell a
  record was pushed PUBLIC. Add the client, the fields and the warning. Prefer
  moving the
  view's work into a `WorkerCommand` through `security/queue.py`; CLAUDE.md
  already flags this view as in-request.
- **Tests.** 200 (PUBLISHED with a date), 404, 5xx and timeout give "unknown",
  logged at INFO.
- **Risks.** Another synchronous call if the view is not moved.

### 5.12 Check the fork before launching the fix agent

- **What.** Before `create_cursor_agent`, run
  `git ls-remote --symref <fork_url> HEAD refs/heads/<base>` with
  `GIT_TERMINAL_PROMPT=0`.
  - The base branch is on the fork: launch as today.
  - The fork exists but lacks the base branch: start from the fork's HEAD
    branch and note it. The prompt already branches off `upstream/<base>`.
  - Missing or private, and the fork name was derived: refuse, naming
    `security_triage.fix_agent.fork`.
  - Anything else, or an explicit fork that may be private: WARNING, then
    launch.
- **Why.** `fork_full_name` (`security/fix_agent.py:161`) formats
  `<github_username>/<repo>` without checking, and `startingRef` is resolved on
  the fork (450). GitHub forks do not receive branches cut upstream after the
  fork was made. Magpie's issue-fix requires that the fork exists and never
  creates one.
- **Files.** frank: `security/fix_agent.py` (`launch_fix_agent` at 406,
  `remote_default_branch` at 127, `find_fix_branches_on_fork` at 504),
  `tests/security/test_fix_agent.py`. Magpie:
  `plugins/magpie-security/skills/issue-fix/SKILL.md:175-176`,
  `pre-implementation.md:138-141`.
- **Pin.** None (port of a precondition); same text at 0.1.0 in
  `skills/security-issue-fix/SKILL.md`.
- **Remote path:** web host only; the same `git ls-remote` pattern the module
  already uses.
- **Steps.**
  1. Add the check and the four outcomes above.
  2. Treat an auth prompt as "missing or private". Unauthenticated HTTPS against
     a missing GitHub repo answers with an auth prompt, not "Repository not
     found".
  3. Set `GIT_TERMINAL_PROMPT=0` on the two existing `ls-remote` calls too,
     which today can wait on a terminal under `make serve`.
- **Tests.** Patch `subprocess.run` as the `find_fix_branches_on_fork` tests do,
  and cover all four outcomes.
- **Risks.** About a second added to an in-request launch.

### 5.13 AI-authorship body signals

- **What.** `scoring/ai_signals.py` ports Magpie's eight regexes: the four
  `_AI_SIGNALS`, `_DISCLOSURE_HEADING`, `_STRUCTURED` and the two in
  `_AFFIRMED`. Tool
  trailers and first-person tool phrasing set `likely_ai_generated`. The weak
  shapes (a test-plan task list, an echoed title) only feed a
  disclosure-missing nit in `pr_description.scan`, and only when the project's
  PR template asks for disclosure. It never gates.
- **Why.** `likely_ai_generated` is set only from a session link today
  (`backends/poller.py`), and the design promises "commit trailers match".
  The port reads the PR body only; frank has no PR-commits fetcher (4.8), so a
  trailer counts when the description carries it.
  Magpie uses these signals only for the disclosure finding, never for routing,
  and says the finding "must never gate"; routing on the strong signals is
  frank's own extension.
- **Files.** frank: the new module, `backends/poller.py`,
  `review/checks/pr_description.py` (`scan` at 91, `_fetch_template` at 139),
  `scoring/signals.py`. Magpie:
  `tools/pr-management/src/pr_management/code_review/scans.py:89-131`,
  `plugins/magpie-pr-management/skills/code-review/criteria/ai-generated-code-signals.md`.
- **Pin.** None (port). The regexes are not at 0.1.0; the same signals are listed in
  prose in `skills/pr-management-code-review/criteria.md` there. Agent names are
  unioned with
  `ProjectConfig.ai_agents` so the list ages in config.
- **Remote path:** worker host only.
- **Steps.**
  1. Write the module.
  2. In the poller, OR the strong signals into `likely_ai_generated` and store
     each fired name in `score_breakdown` as its own numeric key
     (`pr_detail.html` renders every value through `floatformat`, so a list
     would show blank).
  3. Emit the nit in `pr_description.scan` with no LLM call.
- **Tests.** Each regex. Disclosure affirmed means no nit; no template means no
  nit.
- **Risks.** Humans also write "## Test plan" bodies; hence the split into
  strong and weak signals.

### 5.14 Identity and pointer signals in security dedup

- **What.** Changes to `security/duplicates.py`:
  - `Signature` gains the CVE and GHSA ids found in title and body, and a
    symbol set from Magpie's code-pointer regex.
  - A shared advisory id is an identity match, like the identical-title rule.
  - Symbols get a weight of 0.20.
  - Reporter local-part equality is a tie-break of 0.05 that only raises the
    score.
  - The title-group LLM prompt asks for Magpie's four axes (component, bug
    class, attack path, fix shape). It sees only titles and components today
    (`_render_titles` at 536), so the last two axes need the head of each
    body, as Magpie's sweep reads the first 300 characters.
  - `link_duplicate` still never sets status.
- **Why.** A disclosure forwarded by three people cites one id. That is
  identity, not similarity, and frank barely sees it today: `_WORD_RE` keeps
  the word `cve` and drops the number, so a shared CVE id adds one common
  token to the body overlap.
- **Files.** frank: `security/duplicates.py` (`Signature` at 120, `score_pair` at
  245, `link_duplicate` at 393, the LLM prompt at 492, `llm_duplicate_sweep` at 588), `config/models.py`
  (`SecurityDuplicateConfig` at 1571: `id_match_is_strong`, `symbol_weight`),
  `tests/security/test_duplicates.py`. Magpie:
  `plugins/magpie-security/skills/issue-import/duplicate-search.md:19-127` (not
  `existing-tracker-dedup.md`, which is a different check).
- **Pin.** None (port). 0.1.0 has no `plugins/` tree, but the same rules (GHSA ids,
  code pointers, the four axes, the reporter local-part) are in
  `skills/security-issue-import/SKILL.md` there.
- **Remote path:** worker host, and the web process for the re-run button
  (`security_report_rerun_duplicates` sweeps in-request); the LLM sweep goes
  through whichever triage
  backend is configured, in argv when it is agent-cli.
- **Steps.**
  1. Add the id and symbol regexes, keeping frank's broader `_PATH_RE`.
  2. Apply the weights and the identity match.
  3. Change the prompt wording.
- **Tests.** A shared GHSA id links; a reporter alone does not; re-tune against
  the existing fixtures.
- **Risks.** A scanner vendor sends many unrelated reports from one address, so
  the reporter signal stays a tie-break. On a relayed report `reporter_email` is
  the relay's address (`security@apache.org`, `notifications@github.com`), so
  the comparison skips rows with a `relay_kind`.

### 5.15 Disclosure-window badges on the security list

- **What.** `SecurityTriageConfig.disclosure` with `window_days` (90),
  `grace_period_days` (14) and `reporter_response_timeout_days` (14). The list
  view computes three badges and a governance filter:
  - `overdue_for_disclosure`;
  - `approaching_window_end` (at 80%);
  - a frank-named `advisory_due`: fix released, grace passed, no published
    record. This is a reinterpretation of Magpie's `grace_period_expired`.

  A `reporter_silent` flag uses `external_notes_at` until 6.3's
  `reporter_notified_at` exists. That field is when the review sheet's notes
  column last changed; frank records no contact with the reporter today.
- **Why.** A CVD deadline is what a solo maintainer with a backlog forgets.
  Magpie's sync computes the same flags from its tracker.
- **Files.** frank: `config/models.py` (`SecurityTriageConfig` at 1796),
  `dashboard/views.py` (`_security_tab_q` at 1094, `security_report_list` at 1135),
  the list template. Magpie:
  `plugins/magpie-security/skills/issue-sync/SKILL.md:240-251`, `gather.md:51-57`,
  `organizations/ASF/organization.md:120`.
- **Pin.** None; cite the files at dadb315caa8b881fc1d2195bef03223dac25915a.
- **Remote path:** web process only.
- **Steps.** First receipt is `email_received_at`, else `created_at`. frank has
  no "announced" state, so use 5.11's public state. `cve_record_state ==
  "PUBLIC"` says only that the record was pushed to the ASF service, not that
  cve.org shows it (5.11). The thresholds default silently, as Magpie's do.
- **Tests.** Each badge, from fixed timestamps.
- **Risks.** `external_notes_at` is a weak proxy for reporter contact (any
  sheet comment moves it) until 6.3 lands.

### 5.16 Directive phrases and a parseable injection finding

- **What.** Add Magpie quick-merge's `INJECTION` phrases to
  `security/malicious_prompt._PATTERNS` as a medium-severity
  `directive-shaped-text` pattern: "safe to merge", "skip the review", "you are
  now", "new instructions", "surface this as". Run the regex stage, which needs
  no backend, by default when a PR is processed, and log hits at INFO. Give the
  review prompts (`review/prompt.py:198`, `review/agent_cli.py:231, 241`) a last
  sentence that asks for a `security: prompt-injection attempt` finding at the
  line rather than "say so in one sentence". `UNTRUSTED_CONTENT_RULE` is shared
  with the report parse and triage prompts (`security/prompt.py:60, 200`), which
  return one JSON object and no findings, so they keep the current sentence.
  Today the JSON review parser drops that sentence, and the agent-CLI block
  parser (`review/coderabbit.parse_prompt_only_output`) keeps it as a pending
  draft with no file or line; when it comes before the first block, the two
  become one such draft and the finding loses its anchor.
- **Why.** The rule itself shipped in 4.4. What remains is detection of
  steering text, and keeping the model's report of it.
- **Files.** frank: `security/malicious_prompt.py` (`_PATTERNS` at 58,
  `assess` regex-only at 260-283), `core/untrusted.py:32-42`, the worker's PR
  processing, `review/checks/malicious_prompt.py` (the opt-in
  `malicious-prompt` check, which already runs `assess` when a project lists it
  in `llm_checks`), `tests/security/test_malicious_prompt.py` (the patterns),
  `tests/review/checks/test_malicious_prompt.py`. Magpie:
  `tools/pr-management/src/pr_management/quick_merge/screen.py:65-70`.
- **Pin.** None (port); cite `screen.py` at
  dadb315caa8b881fc1d2195bef03223dac25915a.
- **Remote path:** worker host for the regex; the sentence rides argv.
- **Steps.** Add the pattern, the call site and the sentence.
- **Tests.** Each phrase hits; ordinary prose ("this is safe to merge after the
  rebase" from a maintainer) is a known false positive and is recorded as data,
  not acted on.
- **Risks.** Noise in the INFO log on repositories whose templates talk to
  agents. `_PATTERNS` is shared: a new pattern also lands in the report-intake
  callout (`security/intake.py`) and in the verifier's and fix agent's
  `refuse_on_injection` gate. On a project that lists `malicious-prompt` in
  `llm_checks`, a medium hit alone is verdict "maybe", which `is_bad` counts, so
  with no backend configured a PR description saying "safe to merge" files a
  SecurityReport and an important draft. Keep the phrases in their own tuple
  for the new call site, or accept that.

### 5.17 Credit typing for every credit, and the prior-public-fix rule

- **What.** Port `is_bot_credit` (Magpie's known-name list, the `[bot]` suffix
  rule and eight patterns) into `security/credits.py` and use it in `_credits`
  for every `reporter_credit`, not only through the relay's `credit_kind` hint.
  No form writes `reporter_credit` today (only the three intake doors do), and a
  credit the operator types into the preview JSON is pushed as written. Add
  Magpie's Rule 1: no finder credit when a public fix PR was opened before the
  report. frank has no lookup for that PR today: `fix_landed._find_pr` finds the
  PR from frank's own fork branch, which is opened after the report, and keeps
  only its URL and merge time. The operator gives the earlier PR's URL, and
  frank reads its `created_at` once and compares it with the report's
  `email_received_at` or `created_at`. Unknown timing means `finder`, and the
  operator decides.
- **Why.** 4.6 types relay credits from the adapter's kind hints only. Rule 2
  (omit rather than write "anonymous") is already how `_credits` behaves.
- **Files.** frank: `security/cve_advisory.py` (`_credits` at 93),
  `data_access/email_inbox/parser.py` (`_CREDIT_KIND_HINTS` at 111),
  `security/fix_landed.py`, the advisory template. Magpie:
  `tools/cve-tool-vulnogram/generate-cve-json/src/generate_cve_json/cve_json.py:358-435`,
  `tools/cve-tool-vulnogram/finder-credit-policy.md:31-45` (not at 0.1.0),
  `bot-credits-policy.md` (at 0.1.0).
- **Pin.** None. Port the lists verbatim with attribution. If 5.5's extra is
  installed, import `is_bot_credit` from it so the lists do not drift.
- **Remote path:** worker and web processes; `_credits` runs in the advisory
  preview view. Nothing on the agent host.
- **Steps.**
  1. Write the module.
  2. `_credits` uses it.
  3. Store the Rule 1 decision with one line of explanation on the page.
- **Tests.** Known names, `*-bot`, `*.ai`; a PR older than the report means no
  credit; unknown timing means `finder`.
- **Risks.** `.ai` matches a human with an `.ai` domain; it is a typing hint
  the operator sees, not a verdict. Rule 1 has exceptions a timestamp cannot
  see (a later report on a different prong, a credit already offered in
  writing; `finder-credit-policy.md:60-83`), so its result is shown as a
  suggestion the operator confirms.

### 5.18 Feed `committer_is_on_it` from the reviews frank already reads

- **What.** Pass `recent_reviews` into scoring from the reviews
  `_sync_merge_intent` already reads. When `committers:` is empty, count a
  reviewer whose review carries `author_association` OWNER, MEMBER or
  COLLABORATOR (Magpie's `MAINTAINER_ASSOC` set) as a committer, and log that
  once per project at INFO.
- **Why.** `score_committer_is_on_it` (the design's 48h derank) runs only when
  `recent_reviews` is passed (`scoring/scorer.py:58, 161`), and neither poller
  call passes it (`backends/poller.py:479, 1187`; nothing else calls the
  scorer), so the derank cannot fire. This is the second
  half of the author-association survivor; the first half is 4.17.
- **Files.** frank: `backends/poller.py` (the reviews read at 745),
  `scoring/scorer.py`, `scoring/signals.py`. Magpie:
  `tools/contributor-metrics/src/contributor_metrics/backends/github.py:27`
  (`MAINTAINER_ASSOC`).
- **Pin.** None; cite the file at dadb315caa8b881fc1d2195bef03223dac25915a (not
  at 0.1.0).
- **Remote path:** worker host only.
- **Steps.** Thread the reviews through as `{"reviewer": login}` entries
  filtered to 48 hours; the signal leaves the window to its caller. In
  `ingest_single_pr` scoring (1187) runs before `_sync_merge_intent` (1206), and
  `_sync_merge_intent` skips the read when merge intent is off or has no
  approvers, so the reviews read moves out of it and ahead of scoring.
  MEMBER on `apache/*` is broader than
  the project's committers, so use it only when the list is empty. NONE means
  "not a maintainer", not "unknown".
- **Tests.** The derank fires with a configured committer and with a MEMBER
  review and an empty list; it does not fire for a NONE reviewer.
- **Risks.** Org membership is a coarse proxy, and the log line says so.

### 5.19 Suggest the review event instead of always posting COMMENT

- **What.** `suggest_review_event(pr, drafts)` in `backends/poster.py` ports
  Magpie's severity rules: a critical draft, or two or more important ones,
  suggests REQUEST_CHANGES; otherwise COMMENT. When `pr.mergeable is False` the
  form names the conflict and the body gets nothing new: frank already files an
  `auto-conflict` draft for a conflicted PR (`review/drafter.py:86, 682`). A radio on the post form defaults to the
  suggestion and lists the gates that keep APPROVE off. APPROVE is never the
  default and is hidden on the operator's own PRs. The auto-poster stays
  COMMENT.
- **Why.** Every frank review is a COMMENT (`backends/poster.py`, `post_review`;
  `dashboard/views.py:692`). Both lenses found the APPROVE gate inert because
  CI and reviews were never read. Both are read now: `list_pull_request_reviews`
  at `poller.py:745`, and `ci_status` at 774 for intending-to-merge and own
  PRs. `ci_status` is stored, so the gate list can say "CI fail" or "CI not
  reported". The reviews are stored only as `approval_count` and the merge
  intent, so "standing CHANGES_REQUESTED by X" takes one reviews read when the
  form renders (Magpie's `other_changes_requested`, `disposition.py:122-132`).
- **Files.** frank: `backends/poster.py`, `dashboard/views.py`, the post-form
  template, `tests/test_github_poster.py`. Magpie:
  `tools/pr-management/src/pr_management/code_review/disposition.py:56-132`.
- **Pin.** None (port); not at 0.1.0. The gate semantics are at 0.1.0 in
  `skills/pr-management-code-review/review-flow.md`.
- **Remote path:** web process only.
- **Steps.** Add the function and the radio, and pass the chosen `event` from
  the view (`dashboard/views.py:727`); `GitHubPoster.post_review` already takes
  one (`backends/poster.py:192`, default COMMENT).
- **Tests.** Each rule; the operator's own PR hides APPROVE; missing CI suggests
  COMMENT.
- **Risks.** See question 1 on what "CI pass" can mean. GitLab's
  `create_review` ignores `event`, so the radio shows only for GitHub and Gitea
  projects.

### 5.20 Structural "class-project" signals as a note-only flag

- **What.** `scoring/slop.py` ports Magpie's regexes and `h1`, `h2`, `h5`, `s1`,
  `s2` and `s4`, plus `outcome()`, without H3, H4, S3 and S5: frank has no commit
  list and no CI contexts. The result is a `likely_slop` chip and `needs-triage`
  routing. It never skips a review and never drafts a warning, because Magpie
  itself leaves H1, H5 and S2 to judgement.
- **Why.** `low_context` is frank's only body-shape signal. Fork-dump and
  class-project PRs are common on popular repos.
- **Files.** frank: the new module, `scoring/moderation.py`
  (`compute_moderation_flags` at 40), `backends/poller.py` (routing; keep each
  file's `status` from the files API to compute H1). Magpie:
  `tools/pr-management/src/pr_management/code_review/slop.py:41-210`,
  `tools/pr-management/tests/code_review/test_slop.py`.
- **Pin.** None (port). The rules are at 0.1.0 in
  `skills/pr-management-code-review/slop-detection.md`; the code is not.
- **Remote path:** worker host only.
- **Steps.** Port, add the flag and the route, show the fired signal ids.
- **Tests.** Magpie's cases 1, 3, 5 and 8 keep their outcome without H3, H4,
  S3 and S5. Cases 2 and 9 drop from early-exit to note-only and case 4 from
  note-only to silent, so they are ported with the recomputed outcome. Cases 6
  and 7 rest on commit signals; case 6, the injection case, comes out silent
  here, which is the cost of having no commit list.
- **Risks.** Monorepos trip H5; hence note-only.

### 5.21 Markdown scan-archive adapters

- **What.** Two adapters in `security/scan_archive.py` after the JSON manifest
  path. The first is Magpie's markdown findings blocks: `# Title`,
  `**Severity:**`, split on `^---\s*$`. The second is the ASVS pair:
  `issues.md` blocks and `consolidated.md` sections. Each produces one report
  per finding.
- **Why.** An archive of markdown findings with no JSON falls through to
  `zip_import`'s per-file path today, so one `findings.md` holding 30 findings
  becomes one report. That is the problem `scan_archive.py` was written to fix
  for JSON.
- **Files.** frank: `security/scan_archive.py` (`_FINDINGS_KEYS` at 66,
  `MAX_FINDINGS` at 88, `expand_scan_archive` at 234),
  `tests/security/test_scan_archive.py`. Magpie: `tools/scan-format/README.md:64-109`
  (at 0.1.0), `plugins/magpie-security/skills/issue-import-from-md/findings-format.md:10-70`
  (split out after 0.1.0; the same recipe is inline at 0.1.0 in
  `skills/security-issue-import-from-md/SKILL.md:249-309`).
- **Pin.** None; regexes ported with a citation at
  dadb315caa8b881fc1d2195bef03223dac25915a.
- **Remote path:** web process (the upload runs in-request) and the
  `import_security_zip` command; nothing on the agent host.
- **Steps.** Markdown blocks first, ASVS second. Each keeps `MAX_FINDINGS` and
  reads through the caller's budgeted reader, so `MAX_TOTAL_BYTES` still applies. If nothing parses, log INFO and fall through to per-file import.
- **Tests.** A fixture for each format, and a block whose body contains a heading.
- **Risks.** Markdown heuristics are brittle; the fall-through keeps today's
  behaviour.

### 5.22 Attach reporter follow-ups to their report

- **What.** The parser captures `In-Reply-To` and `References` as normalised
  tokens. The email poll resolves them against `email_message_id` before
  classifying, and a hit appends a follow-up to the existing report instead of
  filing a new one. An optional SINCE window
  (`security_triage.email.search: unseen | since`, `since_days` 14) catches a
  root the operator read in a mail client before frank polled.
- **Why.** `fetcher.py:92` searches `UNSEEN` only, and the poll dedups by
  Message-ID alone (`worker/runner.py:2920-2948`). A reply on the thread therefore
  becomes a second report that `duplicates.py` may link, and a root read on a
  phone first is never ingested.
- **Files.** frank: `data_access/email_inbox/fetcher.py` (`_HEADER_PEEK` at 52,
  search at 92), `parser.py`, `types.py`, `worker/runner.py`, `core/models.py` (a
  follow-up model and a normalised, indexed message-id column, with
  migrations). Magpie: `tools/mail-source/imap/src/imap_source/threads.py:40-95`,
  `searching.py:48-65` (landed `3c86798e`, after 0.1.0).
- **Pin.** None. Port the chain logic, not Magpie's fetch: frank keeps
  `BODY.PEEK` and the read-only select.
- **Remote path:** worker host only.
- **Steps.** Land the SINCE window and header capture first, the follow-up model
  second. `MAX_MESSAGES_PER_POLL` bounds the window. Subject threading is not
  ported: Magpie's `tools/gmail/threading.md:224-229` warns that a generic subject
  collides with unrelated threads.
- **Tests.** A reply by In-Reply-To alone attaches; a broken chain takes today's
  path; a server that rejects SINCE falls back with a WARNING.
- **Risks.** A new model; mailing-list software sometimes rewrites References.

### 5.23 Nudge before suggesting close, and never in our court

- **What.** Split `likely_unowned` into `stale_nudge` and `stale_close`,
  measured from last real activity rather than `created_at`. Use PR thresholds
  (45, 90, 180 days) and recognise a standing nudge by frank's own marker and by
  Magpie's `<!-- pr-stale-sweep-nudge -->`. Apply Magpie's notice floor. Never
  flag when 4.16's `author_awaiting_maintainer` fires; keep today's draft and WIP
  exclusion and add bots. A nudge draft is offered on `stale_nudge` only, a close suggestion on
  `stale_close` only.
- **Why.** `moderation.py:104-111` flags any PR older than 14 days with no
  requested reviewers that is not a draft or WIP-titled. That includes one whose author asked a maintainer and
  got silence, which punishes the contributor for our delay. The right Magpie
  source is the PR sweep, not the issue sweep.
- **Files.** frank: `scoring/moderation.py` (`_DEFAULT_UNOWNED_DAYS` at 16),
  `backends/poller.py` (routing), `config/models.py` (a `StaleConfig` on
  `ProjectConfig`), `tests/scoring/test_moderation.py`. Magpie:
  `tools/pr-management/src/pr_management/stale_sweep/classify.py:57-60, 117-212`,
  `plugins/magpie-pr-management/skills/pr-stale-sweep/classifications/`.
- **Pin.** None (port). `classify.py` is from 2026-10-10 (`be111f86`); the
  maintainer-court rule is at 0.1.0 in `skills/pr-stale-sweep/SKILL.md`.
- **Remote path:** worker host only.
- **Steps.** Compute at refresh from the comments the poller already fetches,
  persist the flags, and keep the stored flag when a fetch fails.
  `REVIEWER.md`'s "30 days" is guidance for frank's own repository, not a
  default.
- **Tests.** Activity resets the clock; a nudge marker moves to `stale_close`;
  an author waiting on us is never flagged.
- **Risks.** Thresholds are per project; the defaults are Magpie's.

### 5.24 Behavioural evals for frank's review prompts

- **What.** Adopt Magpie's eval suite layout and assertion vocabulary under
  `tests/review/evals/`:
  - `fixtures/` plus `case-N/{report.md, expected.json, case-meta.json}`;
  - `assertions.json` predicates: regex, contains, count, max_length, judge;
  - adversarial cases.

  Run the suites through frank's own backends and parsers behind a pytest
  marker and `make eval`, outside CI.
- **Why.** `tests/review/test_prompt.py` checks substrings; nothing checks that a
  prompt change still yields the findings it should, while the anti-pattern loop
  keeps changing what the prompt says.
- **Lenses disagreed.** The fit lens would port the format and run it natively.
  The pin lens would pip-install the runner and add a `manage.py llm_pipe`
  bridge. Native is recommended:
  - Magpie's `collect_diffs` compares lists index by index
    (`runner.py:553-630`), so a reordered findings array fails;
  - the runner scavenges JSON instead of using frank's parser;
  - frank's `agent_cli_backend` already runs any configured reviewer over ssh.
- **Files.** frank: `tests/review/evals/`, `review/backends/agent_cli_backend.py`,
  `review/coderabbit.py` (`parse_prompt_only_output`, which `review/agent_cli.py` uses),
  `review/backends/base.py` (`parse_llm_review`, which `agent_cli_backend` uses), `Makefile`. Magpie:
  `tools/skill-evals/README.md:345-418, 461-534`,
  `tools/skill-evals/src/skill_evals/runner.py` (the predicate vocabulary).
- **Pin.** None for the port. The recorded alternative: a `tools:` entry and an
  `evals` extra
  `skill-evals @ git+https://github.com/apache/magpie.git@dadb315caa8b881fc1d2195bef03223dac25915a#subdirectory=tools/skill-evals`
  (dev only, never in the worker image).
- **Remote path:** the harness runs on the developer or worker host and calls
  the configured reviewer through its existing executor, so the prompt rides
  argv and nothing is installed on the agent host.
- **Steps.**
  1. Write the layout.
  2. Write a loader that runs one case through a named backend and parser.
  3. Implement the predicates.
  4. Add three adversarial cases (an instruction in the PR body, in a diff
     comment, in a prior comment).
  5. Run per agent-CLI timeout, not Magpie's 120 seconds.
- **Tests.** The harness's own unit tests on canned outputs. Live runs are
  operator-run; a run that grades nothing is an error, not a pass.
- **Risks.** Judge cost and nondeterminism; failures need a human read.

### 5.25 Normalise the CVE title before allocation

- **What.** `cve_api.normalize_title(title, repo)` ports the generic part of
  Magpie's strip cascade: security tags, identifier parentheticals,
  "split from" notes and the repo name. It pre-fills the allocation form and the
  advisory preview's `cna.title` (`cve_advisory.py:72` uses `report.title` today) with
  the original shown underneath, and warns under three words. The operator's
  edit is authoritative.
- **Why.** Reporter titles carry "[Security Report]" prefixes, and the title
  becomes the public CVE title. Low value: the operator edits it anyway.
- **Files.** frank: `security/cve_api.py` (`allocate_cve` at 197),
  `security/cve_advisory.py` (`cna["title"]` at 72),
  `dashboard/views.py` (`security_cve_allocate` at 3711, `_cve_api_panel_context` at 3607),
  `dashboard/templates/dashboard/_security_cve_api.html` (the prefill at 80),
  `tests/security/test_cve_api.py`. Magpie:
  `plugins/magpie-security/skills/cve-allocate/title-normalize.md:32-104`.
- **Pin.** None (port). At 0.1.0 the cascade was inline in
  `skills/security-cve-allocate/SKILL.md:308-386`.
- **Remote path:** web process only.
- **Steps.** Port only the generic patterns; the cascade is per project upstream.
  Do not add a "reserved-not-saved" status until frank's client is seen to
  receive that shape.
- **Tests.** Each pattern; an over-stripped title keeps the original.
- **Risks.** Over-stripping, which the warning covers.

---

## 6. Tier 3: Borrow the Text or the Design

Survivors that are text or a principle, not yet on this branch, ranked by value
and then effort. Attribution works the same way for all of them:

- **Adapted text** (the usual case): a comment at the point of use naming the
  upstream path, the line range and commit
  `dadb315caa8b881fc1d2195bef03223dac25915a`, and "Apache-2.0"; then add the
  module to the Magpie row in ATTRIBUTION.md. Cite Magpie principles by title,
  not number: PRINCIPLES.md renumbered after 0.1.0 (`bcd8b7f2` inserted a new
  section 1).
- **A byte-exact copy**, only when code reads the body at runtime: add a
  `vendored:` entry (source, dest, sha256, purpose) to `magpie-pin.yaml`, run
  `manage.py magpie_sync --sync`, and read it with `vendored_text()`. The copy
  keeps Magpie's own SPDX header in its body, and `magpie_sync` adds the
  provenance header. The lock lists no vendored files today, so the first such
  entry creates `src/franktheunicorn/magpie/vendored/`.
- **Once, before the first of these lands:** reproduce Magpie's NOTICE
  attribution lines in ATTRIBUTION.md: "Apache Magpie, Copyright 2026 The
  Apache Software Foundation. This product includes software developed at The
  Apache Software Foundation (https://www.apache.org/)." The same NOTICE also
  carries Apache Airflow and Apache Groovy copyright lines for portions that
  originated there; keep any that pertain to the borrowed text. Apache-2.0
  section 4(d) asks for that, and frank has no NOTICE file of its own.

### 6.1 The fix agent treats the scanner patch as a suggestion

- **What.** Rewrite the patch paragraph of `_FIX_PROMPT`:
  - the patch is an untrusted suggestion from outside the project;
  - work out the bug from the description and the code, and check the patch
    against that diagnosis before using it;
  - check for defects that look right: a flipped comparison, an off-by-one
    bound, a widened regex or allowlist, a sanitiser that strips a substring
    once (`../` removal is bypassed by `....//`);
  - the final message says where the change differs from the patch.

  Rename the fence marker to `--- PROPOSED PATCH: UNTRUSTED SUGGESTION ({nonce}) ---`
  and add `PROPOSED PATCH` to `_FENCE_MARKER_RE`, or a forged marker in report
  text is no longer defused. In `recheck.build_valid_check_prompt`, which fills
  `_VALID_CHECK_PROMPT`, label the patch
  the same way ("a correct fix may look different"). The `hx-confirm` and the
  detail page say "writes a fix using the report's patch as a reference".
- **Why.** The patch's only writer is the scanner import
  (`security/zip_import.py:793`). The prompt says "If it seems legit apply the
  patch" (`security/fix_agent.py:313`). A subtly defective patch applied
  verbatim later reverse-applies cleanly, and `branch_scan.scan_already_fixed`
  then stamps the report "likely fixed" with method `git`: the defect certifies
  itself.
- **Lenses disagreed.** Magpie's rule is "do not copy verbatim; re-derive the
  fix". The fit lens recommends the softer form above, which keeps "apply"
  allowed after a check, because re-deriving changes the operator's workflow
  and makes reverse-apply detection answer "unclear". See question 3.
- **Files.** frank: `security/fix_agent.py` (`_FIX_PROMPT` at 311-326,
  `_FENCE_MARKER_RE` at 335), `security/recheck.py:512`,
  `dashboard/templates/dashboard/_security_fix_status.html:64`,
  `security_detail.html`, `tests/security/test_fix_agent.py`,
  `tests/security/test_recheck.py`. Magpie:
  `plugins/magpie-security/skills/issue-fix/pre-implementation.md:121-122`,
  `implementation-plan.md:27`, the eval case
  `tools/skill-evals/evals/security-issue-fix/step-3-fixability/fixtures/case-5-untrusted-snippet/report.md`.
- **Attribution.** Comment above `_FIX_PROMPT`: "Untrusted-suggestion rule
  adapted from Apache Magpie
  plugins/magpie-security/skills/issue-fix/pre-implementation.md L121-122 at
  dadb315caa8b881fc1d2195bef03223dac25915a (file sha256
  c93764ede3b3857fe27886dfe802a5787b76749545dc0b8e169456056c5a4881),
  Apache-2.0; also at tag 0.1.0 in skills/security-issue-fix/SKILL.md
  L434-444." No vendoring: nothing reads the file at runtime.
- **Remote path:** prompt text in the Cursor cloud POST body; nothing on any
  agent host.
- **Effort.** Small. Tests: the prompt carries the rule and the new marker; a
  forged `--- PROPOSED PATCH` line in report text is defused.

### 6.2 A principles map in this document and the design doc

- **What.** Replace the principles paragraph at the end of section 1 with a
  table of at most eight rows. Each row is a Magpie principle by title, frank's
  status (follows, adapts, diverges) and the file that proves it:
  - external content is data: `core/untrusted.py` (4.4);
  - deterministic gates: the anti-pattern gate (`review/antipattern.py`) and the
    deterministic checks;
  - human in the loop: `PostingConfig.mode` defaults to draft-only;
  - impersonation never graduates to an auto-mode (a sentence in "The human is
    always in the loop, until they choose otherwise"): diverges, see below;
  - audit and reverse: `OperatorAction` and the recall window, which covers
    inline comments only;
  - no telemetry: depends on question 2;
  - snapshot plus override, never vendored copies: diverges, because frank
    keeps adapted copies under a sha lock;
  - confined before it reads anything (a claim of Magpie's README, not a
    PRINCIPLES.md title): 4.11 and 4.18.

  Add one sentence to Core Principle 3 in the design doc. Confidence-gated
  posting (`review/auto_poster.py`, posting as a separate bot identity) is a
  deliberate, opt-in divergence from Magpie's "no standing pre-approvals"
  (RFC-AI-0004 P1.3). `MergeQueueConfig.auto_merge` (`config/models.py:1228`,
  read nowhere) must follow a per-class allow-list, never covering
  security-class changes, if it is ever wired.
- **Why.** A reader cannot tell from the design doc that frank's safety posture
  matches Magpie's in most places and where it does not. Writing down the two
  divergences matters before anyone wires `auto_poster` or `auto_merge`.
- **Not a divergence.** The design doc's Tier 3 fine-tuning is not what
  RFC-AI-0004 calls implicit training. It is explicit and operator-run
  (`manage.py fine_tune`), with eval thresholds defined in
  `fine_tuning/evaluator.py:18-22`. That gate is not wired yet: `fine_tune`
  skips evaluation because model inference does not exist
  (`core/management/commands/fine_tune.py:144-156`).
- **Files.** frank: this document's section 1,
  `docs/franktheunicorn-master-design.md` (Core Principles at 62). Magpie:
  `PRINCIPLES.md`, `docs/rfcs/RFC-AI-0004.md:147, 261`.
- **Attribution.** Principle titles plus the commit; short quotes only.
- **Remote path:** docs only.
- **Effort.** Small.

### 6.3 Reporter reply drafts on the report page

- **What.** `security/reporter_reply.py` builds a copyable draft for the
  reporter. The draft covers receipt, invalid or info-only, duplicate,
  defense-in-depth, and fix-already-public. It is filled from the report and
  up to three verbatim quotes the triage step picks (a `quotes` key in the
  triage JSON). It renders as an htmx fragment and is never sent; frank's mail
  path stays read-only IMAP. A `reporter_notified_at` field records the
  operator's tick after sending by hand. Project wording can be overridden in a
  frank-authored `canned-responses.md` named by `security_triage.canned_responses_file`.
- **Why.** Every reporter is answered by hand today and nothing helps. Magpie
  encodes the discipline: the canned section verbatim, additions marked as
  additions, the reporter's CVSS never echoed, no tracker contents in mail.
- **Files.** frank: the new module, `security/prompt.py`, `core/models.py` (a
  field, a migration), `dashboard/views.py`, a new template. Magpie:
  `plugins/magpie-security/skills/issue-triage/classification.md:143-156`
  (the one complete generic template, the fix-already-public reply),
  `plugins/magpie-setup/templates/canned-responses.md:169-185` (drafting rules),
  `plugins/magpie-security/skills/issue-invalidate/reporter-draft.md:79-95`.
- **Attribution.** Only one Magpie template is generic; every other body
  belongs to the adopting project, so ship frank-written examples and label them
  as such. If `classification.md` and `canned-responses.md` are read at
  runtime, add `vendored:` entries and read them with `vendored_text()`.
  Otherwise use adapted text with the comment.
- **Remote path:** web process; the `quotes` key rides the triage prompt's argv
  when triage uses an agent-cli backend.
- **Effort.** Medium (new code, a field, a fragment).

### 6.4 One or two sentences of positioning in README and the design doc

- **What.** A Magpie entry in README's "Similar Tools & Related Projects" (line
  251) and a "Relationship to Apache Magpie" item in the design doc's "Prior Art
  & Lineage" (line 76). Each is one or two sentences linking to section 1 of
  this document, which stays the only long form. Add the sentence that matters
  for the maintainer: frank drives headless, read-only agent CLIs on a remote
  host over SSH using each CLI's own login, while Magpie skills run inside the
  maintainer's own interactive agent.
- **Why.** README does not mention Magpie, and the design doc names it only in
  the scoring table (line 370). A long paragraph in three places would drift
  from a project that lands commits daily.
- **What not to claim.** Do not claim "no telemetry" until
  question 2 is settled. Do not say Magpie's rules are pinned prompt material
  under `src/franktheunicorn/magpie/`: the lock lists no vendored files, and the
  adapted text lives in the modules ATTRIBUTION.md names. Do not say every post
  has a recall window: recall covers inline comments, not review bodies. Magpie's
  Triage mode is stable for the security family only, and Meta is also stable.
- **Attribution.** Status claims are dated to the pinned commit (2026-10-11).
- **Remote path:** docs only.
- **Effort.** Small.

### 6.5 The PoC policy in writing, and an honest sandbox label

- **What.** The sandbox result template says "script exited 0; this is not
  proof of the vulnerability" where it now says "confirmed". The stored value
  (`security/sandbox.py:107`, `SecurityReport.sandbox_verdict`) stays, so no
  data migration is needed. Add a paragraph to `docs/security-design.md`
  section 4.1 adopting Magpie's four rules: never on the host; static read
  first (frank's verifier, section 4.10); an isolated container only on
  explicit approval; never execute binary attachments. Cross-check
  `sandbox.py` for credential mounts and port forwards; network, read-only
  root and tmpfs already match.
- **Why.** These are the residuals of 4.7 and of the dropped body-sanitisation
  candidate. "Confirmed" over-states a script that merely echoes.
- **Also found.** `security/sandbox.py` shells out to a `docker` binary (73,
  131), and the worker image installs only git, ca-certificates and curl
  (`docker/Dockerfile`, worker stage). The PoC sandbox cannot run inside the
  compose worker as built. Installing the CLI is not enough on its own:
  through the mounted host socket, `-v` sources resolve on the host, where the
  worker's temp script and clone paths do not exist. Fix both or say so in
  the paragraph.
- **Files.** frank: `dashboard/templates/dashboard/_security_sandbox_result.html`,
  `docs/security-design.md`, `tests/security/test_sandbox.py`. Magpie:
  `docs/security/poc-handling-policy.md:26-60` (after 0.1.0).
- **Attribution.** A cited paragraph; the template carries the same citation in
  a comment.
- **Remote path:** worker and web only; the sandbox uses the worker's Docker.
- **Effort.** Small.

### 6.6 Report agent runs the way Magpie reports costs

- **What.** The stats page breaks `CostRecord` down by action type and by
  backend. A "not metered" line counts agent-CLI runs, labelled as billed to
  the CLI's own account, and the digest's cost line carries the same caveat.
  Record one row per agent run (action `agent-cli-run`, backend
  `<reviewer>@<host or local>`, zero tokens, the elapsed time `agent_cli.py`
  already measures), so the page shows run counts and wall time. p50 and p90
  per PR are optional.
- **Why.** Agent-CLI runs never write a `CostRecord`. The backend keeps tokens
  at zero on purpose (`review/backends/agent_cli_backend.py:276-279`), and a
  zero-token call writes no row (`tests/review/test_cost_metering.py:95-100`). In
  the maintainer's deployment, where the reviewers are agent CLIs over SSH,
  `/stats` reads near $0 while the real spend sits on the CLI accounts. That is
  the "not measured read as zero" failure Magpie's cost page warns about.
- **Files.** frank: `dashboard/views.py` (`stats` at 930),
  `dashboard/templates/dashboard/stats.html`, `digest/service.py:142`,
  `review/agent_cli.py`, `review/backends/agent_cli_backend.py`,
  `security/verifier.py`. Magpie: `docs/mode-economics.md:35-39, 207-213`.
- **Attribution.** The "not measured does not mean zero" wording is cited.
  Copy no numbers: the page changes often.
- **Remote path:** timing is measured on the worker around the existing
  `executor.run()`; nothing new on the agent host. Test: an ssh-mode run with a
  patched `subprocess.run` writes one row naming the host.
- **Effort.** Small to medium.

### 6.7 An adversarial review focus for agent-CLI reviewers

- **What.** `review_focus: adversarial` (`config/models.py:329` widens the
  `Literal`) selects a third template in `review/agent_cli.py` that frames the
  review as "prove this diff is wrong": auth, data loss, races. The dashboard
  shows "flagged by N reviewers" from `ReviewDraft.sources`, which today is
  rendered only for CodeRabbit.
- **Why.** frank already runs several CLIs per PR, but every general entry gets
  the same instructions. Magpie's multi-agent review has three axes
  (correctness, security, conventions), and none is new to frank.
  "Correctness" is what `_PROMPT_TEMPLATE` already asks, "security" is
  `review_focus: security`, and "conventions" contradicts
  `REVIEWING_GUIDANCE` ("formatting, naming, and import order belong to the
  linter"). What is new is the adversarial framing of code-review's
  second-reviewer pass.
- **Lenses disagreed** on scope; the pin lens kept all three axes.
- **Files.** frank: `config/models.py`, `review/agent_cli.py` (templates at 63
  and 107, selection at 223), the draft template. Magpie:
  `plugins/magpie-pr-management/skills/code-review/adversarial.md:29-33, 215-229`,
  `plugins/magpie-pairing/skills/multi-agent-review/SKILL.md:106-112`.
- **Attribution.** frank's own template with a comment citing `adversarial.md`.
- **Remote path:** the template rides argv like the existing two. Test: an
  ssh-mode run of an `adversarial` entry carries the new template in the remote
  command string.
- **Effort.** Small; each extra entry is one more agent run the operator opts
  into.

### 6.8 Cheap repository hygiene in CI

- **What.**
  - `typos`, pinned exactly in the dev extra (Magpie uses 1.50.3), with a
    `.typos.toml` that ignores bare 40-hex shas and sha256 digests. Magpie's
    ignores backticked shas and two named digest fields only.
  - `lycheeverse/lychee-action`, pinned to a full commit sha, run `--offline`
    with fragment checks over `*.md`.
  - `.github/dependabot.yml` for `github-actions` and `pip`, weekly, grouped,
    with `cooldown: default-days: 7`.
  - `zizmor` optional: frank's two workflows already use least-privilege
    `permissions`.

  Skip RAT and SPDX stamping (an ASF release rule), doctoc, and doc-count sync.
- **Why.** frank's docs are heavily cross-linked and mostly agent-edited. Its
  `pyproject.toml` caps many dependencies with no lockfile.
- **Files.** frank: `.github/workflows/ci.yml`, `.github/dependabot.yml`,
  `pyproject.toml`, `.claude/hooks/pre-push-lint.sh` (the hook gates commits
  too, so keep anything added there cheap). Magpie: `.pre-commit-config.yaml:194-235`,
  `.lychee.toml`, `.typos.toml`, `.github/dependabot.yml`.
- **Attribution.** Configuration ideas only; no text is copied.
- **Remote path:** CI and the developer machine only.
- **Effort.** Small.

### 6.9 A readiness list above the CVE advisory preview

- **What.** `cve_advisory.readiness(report)` lists what is missing before a
  push: CWE, affected versions other than "unknown", a credit decision, a public
  summary naming an upgrade-target version, and a fix reference. The list
  renders above the preview, and the push stays enabled behind an explicit
  override.
- **Why.** frank will push a record whose affected entry literally says
  "unknown". Magpie gates review-readiness on six mandatory fields.
- **Sequencing.** Three of the six fields exist only after 5.5 and 5.17.
  A state-based second stage can read `cve_record_state`, which now knows
  DRAFT, REVIEW, READY and PUBLIC.
- **Files.** frank: `security/cve_advisory.py`,
  `dashboard/templates/dashboard/_security_cve_advisory.html`,
  `dashboard/views.py` (`security_cve_advisory_push` at 3927). Magpie:
  `plugins/magpie-security/skills/issue-sync/signals-to-actions.md:185-265`,
  `apply-and-push.md:383-433`, `tools/cve-tool-vulnogram/record.md:91-122`.
- **Attribution.** Docstring citation.
- **Remote path:** web process only.
- **Effort.** Small.

### 6.10 Where report text goes

- **What.** A new section 4.11 in `docs/security-design.md` lists every egress
  for a stranger's report text, with the setting that controls each:
  - the triage backends, in fallthrough order (`triage._get_triage_backends`
    puts `security_triage.llm_backend` first, then every `llm_backends` entry,
    then every enabled agent-CLI reviewer);
  - the verifier's agent CLI;
  - the Cursor cloud fix and recheck agents;
  - the CSV export;
  - in ssh mode, the agent host itself, where the prompt travels in argv or a
    base64-staged script and outputs pass through `mktemp` files.

  Magpie's zero-data-retention note goes in as an operator note, marked as
  Magpie's provisional position pending ASF Legal. One WARNING at worker start
  fires when the fallthrough includes an agent-cli entry in ssh mode or a
  non-local, non-primary API host; it names the host and
  `security_triage.llm_backend`.
- **Also fix.** Section 4.8 of `docs/security-design.md` says telemetry is
  enabled by default (see question 2).
- **Files.** frank: `docs/security-design.md`, `security/triage.py`,
  `config/examples/operator.yaml`. Magpie: `tools/privacy-llm/models.md:152-155`,
  `docs/setup/privacy-llm.md:317-357`, `AGENTS.md:922-939`.
- **Attribution.** Quoted sentences with the commit.
- **Remote path:** docs plus one log line on the worker; never a gate.
- **Effort.** Small.

### 6.11 Layer table and numbered mitigations in the security design

- **What.** This covers two survivors that edit the same document. Number the
  existing mitigations table (`docs/security-design.md` section 6, at 687) and
  point each row at its module and test. Add a Layer 0 to 3 table in Magpie's
  vocabulary, with a column for where each layer runs in ssh mode:
  - Layer 0, clean environment: none until 5.6 lands;
  - Layer 1, filesystem: containers for tests and PoC runs, none for agent CLIs;
  - Layer 2, tool permissions: 4.11's argv and 4.18's guard;
  - Layer 3, confirmation: dashboard approval.

  The table names only mechanisms that exist, and marks planned ones as
  planned.
- **Why.** Which layer is enforcing a given rule has to be reconstructed per
  feature today. The roles split in Magpie's threat model does not apply to a
  single-operator tool.
- **Files.** frank: `docs/security-design.md`. Magpie:
  `docs/setup/secure-agent-internals.md:77-97`, `docs/security/threat-model.md:568-612`.
- **Attribution.** A one-line citation of both files.
- **Remote path:** docs only.
- **Effort.** Small.

---

## 7. Interop and Contributions Upstream

No interop candidate survived verification. The tracker readers, the adopter
stale-sweep and repo-health config reader, and the release-trains reader are in
section 8. The only interop piece left is the anti-pattern export in 7.7, and it
waits for a project that runs both tools.

Contributions go to apache/magpie under Apache-2.0, which is also frank's
licence. Magpie's `AGENTS.md:512-533` forbids a `Co-Authored-By:` trailer naming
an AI agent and requires `Generated-by: <agent name and version>`, and its
agent-guard `commit-trailer` guard blocks the `Co-Authored-By:` trailer in a Magpie
checkout where Magpie's secure setup is installed. A commit to
Magpie carries `Generated-by:`, not the `Co-Authored-By:` trailer frank's own
commits carry. Open an issue first
for anything larger than a fix, because upstream moves daily.

### 7.1 Three regex fixes found while porting (send first)

- **The ASF relay preamble misses wrapped mail.** The `asf-security` adapter's
  `preamble_match` in `tools/forwarder-relay/README.md:330` has `\s+` only after
  "Dear PMC," and literal spaces after that. Mail clients wrap the template at
  72 columns, so every wrapped copy is missed. frank matches word by word with
  `\s+` (`data_access/email_inbox/parser.py:88-91`).
- **The credit regex stops at the first period.** At `README.md:334`,
  `This vulnerability was discovered and reported by (?P<credit>.+?)\.` turns
  "bugbunny.ai", the README's own example, into "bugbunny". frank reads to the
  end of the line and drops one trailing period (`parser.py:103`).
- **A bare `@bob` alone on a line goes out live.** `_DECORATOR` in
  `tools/pr-management/src/pr_management/code_review/body.py:41`
  (`^\s*@[A-Za-z_][\w.]*(\(|$)`) treats it as a Python decorator, so it is
  neither escaped nor scanned. frank requires a dot or a parenthesis
  (`backends/mentions.py:35-36`).

Each is a one-line pattern change with a test; frank's tests for the same
cases are the reference.

### 7.2 Agent-confinement findings (report privately first)

- **The `--disallowedTools` denylist leaks.** `CLAUDE_DENIED_TOOLS`
  (`tools/adversarial-review/src/adversarial_review/backends.py:55`) left 34 tools
  in a claude 2.1.299 session, including `Monitor`, `Workflow` and
  `SendMessage`. `--tools Read,Grep,Glob` is an allowlist and leaves three
  (4.11).
- **codex `-c mcp_servers={}` removes nothing.** codex deep-merges `-c` into the
  user config, so with codex-cli 0.162.1 a configured server still spawned
  (`backends.py:116`, intent stated at :31). `--ignore-user-config` works (4.11).
- **A reviewed repository's agent config runs code.** The runner starts each
  reviewer with the repository under review as its working directory
  (`runner.py:126`, `ctx.repo_dir`) and moves nothing aside. `--repo-dir` is
  "the git checkout that holds the change" (`cli.py:79`), and the claude argv
  has no `--setting-sources user` (`backends.py:180-191`), so a PR's
  `.claude/settings.json` hook runs at startup (frank reproduced this with the
  same flags and claude 2.1.299, 4.11). The runner has no cursor-agent or pi
  backend, so `.cursor/hooks.json` and `.pi/extensions/*.ts` matter only once
  one is added (7.3).

The third is code execution from an untrusted PR in an ASF project. Whether to report
is the operator's call. If they do, all three go to security@apache.org per ASF
policy before any public issue or PR, with frank's `guard_agent_argv`
(`review/tool_executor.py`) offered as a reference.

### 7.3 A cursor-agent backend and trust-refusal detection for adversarial-review

- **What.** A `_cursor_agent` backend in Magpie's `backends.py` (`BACKENDS` lists
  codex, copilot, gemini, grok and claude). Add `--force` and `-f` to
  `WRITE_GRANTING` in `tests/test_backends.py` (`--yolo` is already there). Classify the "Workspace Trust
  Required" refusal as unavailable in `runner.run_one`, before the
  `_AUTH_ERRORS` check. Separately, fix agent-guard's console script:
  `tools/agent-guard/pyproject.toml:35` binds `agent-guard = "agent_guard:main"`,
  the Claude stdin adapter, so `agent-guard --check git push` from a pip install
  reads stdin. `agent_guard:cli` routes by argument shape and stays backward
  compatible.
- **frank references.** The cursor-agent argv `cursor-agent --trust --mode ask -p`
  (`config/models.py`, with the reason it is `--trust` and not `--yolo`).
  `_TRUST_REFUSAL_MARKERS` and `workspace_trust_advice` in `review/tool_executor.py`.
  pi now has a verified read-only invocation (`--tools read,grep,find,ls`, 4.11).
- **Sequencing.** After 7.2. cursor-agent loads project hooks under `--trust`
  and pi runs project extensions, so adding either backend to a runner that
  moves nothing aside widens the 7.2 hole. Magpie's tests forbid the prompt in
  argv, and cursor-agent's `-p` is a flag with a positional prompt; check stdin
  or brief-file delivery against the binary before opening the PR.

### 7.4 Docs: the 0.2.0 tag, and a commit-anchored lock

- **What.** A docs PR, and an issue for the lock method.
  - `docs/setup/marketplace-install.md:68-69, 212` and
    `docs/setup/marketplace.md:49, 272, 435, 577` name a `0.2.0` tag that does not
    exist, and the dev line has been 0.9.0 since `8ae1427f` (2026-09-25).
  - `docs/quick-start/other-install-methods.md:212-219` still says "v1.0.0 once
    tags exist".
  - The issue proposes a `method: git-commit` lock (url plus commit, no ref, no
    `min_version` semantics) as an amendment to RFC-AI-0007. That RFC already
    specifies a sparse, blob-filtered fetch (`RFC-AI-0007.md:72-80`) but by ref.
    Its example
    `git clone --filter=blob:none --no-checkout --depth 1 <ref> .apache-magpie`
    omits the URL and `--branch`.
- **frank references.** `magpie/sync.py:57-111` (`fetch_snapshot`: fetch by sha,
  then assert `HEAD`), and the measured 61 MB full against 6 MB sparse.
- **frank-side fix done here.** Section 3 no longer shows
  `/plugin marketplace add apache/magpie@0.1.0` as if it worked.

### 7.5 Git proofs for fix state, and batched dedup

- **What.** Two markdown procedures and one option.
  - A "git proof" step in `issue-sync`: reverse-apply the proposed patch before
    any content probe (`git apply --check -R`, where only exit 0 and 1 are
    answers and 128 means the patch did not parse). Then run ancestry and
    `git tag --contains`, with the final-tag rule from 4.20.
  - A pickaxe step in `issue-import-from-scan` that finds the introducing
    commit.
  - The batched title-group sweep as a bulk-mode dedup option alongside
    Magpie's per-report search.
- **frank references.** `security/fix_landed.py`, `security/branch_scan.py:801`
  (`scan_already_fixed`), `security/introduced.py`, `security/duplicates.py:588` (`llm_duplicate_sweep`).
- **Why upstream wants it.** Magpie infers fix state from labels and PR state,
  and re-checks scanner findings by having the agent read code. No security
  skill there uses ancestry, reverse-apply or pickaxe.
- **Effort.** Medium. A Python `tools/git-proofs` package would follow Magpie's
  tool conventions (stdlib-only, a README declaring Capability and
  Prerequisites).

### 7.6 A deterministic injection scanner as a Magpie tool

- **What.** `tools/injection-scan/`, seeded from `security/malicious_prompt.py:58-184`
  (nine named patterns with NFKC and HTML-unescape normalisation). Add the
  hidden-text shapes Magpie's `AGENTS.md:179-185` lists and frank lacks:
  zero-width characters, homoglyph file names, `display:none` spans. Generalise
  frank's `exfil-env` pattern, which names `FRANK_GITHUB_TOKEN`. Wire it as the
  trigger for the "import-time prompt-injection flag" that `issue-import/apply.md:47-49`
  refers to without naming a tool.
- **Lenses disagreed** across areas. This survived with 3/3. A sibling proposal
  from the mentoring area was rejected because Magpie deliberately leaves
  injection detection to the agent
  (`tools/pr-management/src/pr_management/pre_first_pr/checks.py:17-22`). Open
  an issue before writing code.
- **Also report.** `docs/security/threat-model.md:578` (M.2) says import bodies
  are fenced, while `issue-import/apply.md:13-14` inlines them sanitised. The two
  disagree upstream.

### 7.7 The anti-pattern loop and the "+1, don't restate" rule

- **What.** One issue, covering two survivors:
  `contribute-antipattern-loop-and-prior-comment-plus-one` and
  `contribute-rejection-derived-anti-patterns-pattern`.
  - Frame it as implementing RFC-AI-0004 P4's own promise for code-review
    (`RFC-AI-0004.md:253`, "surfaces the override-file path so the correction
    persists"). When the maintainer drops a finding at `[E]dit`, the skill
    proposes one line for `.apache-magpie-local/pr-management-code-review.md`.
    That is the personal layer the skill already reads, per its
    `adopter-config.md`. The line quotes the dropped finding and is confirmed
    item by item, because Magpie's override rules say "inference is not a
    decision" (`docs/setup/agentic-overrides.md:252-259`). Promotion to the
    committed layer goes through `/magpie-setup adopt`, and `override-upstream`
    takes a committed override on to apache/magpie.
  - Add frank's prior-comment rule to `review-flow.md` Step 4. A finding an
    existing comment already makes is dropped when the commenter is the viewer
    or a bot. Otherwise it becomes a backticked "+1 `@login`".
  - Mention a recall step for inline comments; a submitted review cannot be
    deleted.
- **frank references.** `review/antipattern.py`, `dashboard/views.py`
  (`reject_draft`), `review/prompt.py`, `review/prior_comments.py:1-5, 184-213`,
  `review/dedup.py:86-117`, `backends/poster.py` (`recall_comment`).
- **Not offered.** The sklearn rejection predictor (nearly every Magpie
  Python tool is stdlib-only: 33 of 38 declare no dependencies) and frank's database trigger counts (Magpie's state is markdown).
- **Interop.** A `manage.py export_anti_patterns --format magpie` writing that
  file is worth building only for a project that runs both tools.

### 7.8 Two forge notes from frank's clients

- **What.** Two doc additions.
  - A paragraph for `tools/github/operations.md` "Error handling" (313-319): a
    secondary rate limit arrives as a 403 with quota remaining and a
    `retry-after`, and is not an auth failure. Magpie's preflight
    (`operations.md:58-70`) treats any 401, 403 or 404 as a hard stop and asks the
    user to `gh auth login` or to request collaborator access.
  - A Forgejo pull-request review recipe for `tools/forgejo/operations.md`,
    which has none. It should note that `new_position` and `old_position` are
    file line numbers, not GitHub diff positions.
- **frank references.** `backends/github.py` (`_is_rate_limit_response`,
  `_log_auth_suggestions`), `backends/gitea.py` (the position note).
- **Not offered.** The GitLab write subcommands, because Magpie tracks writes
  under its own design (#305) and frank's GitLab path is tested only against
  mocks. Also not offered: a Discourse mail-archive adapter, which would have
  to satisfy all five verbs.

### 7.9 frank's scanner-bundle shape as a scan-format adapter

- **What.** A "patches-manifest" row for `tools/scan-format/README.md`, which
  lists one reference adapter (line 68). It describes the shape:
  - `detect`: a top-level JSON findings manifest plus an optional `PATCHES.json`;
  - `finding_index`: id keys, with severity, verdict, confidence or CVSS mapped
    to priority;
  - `evidence`: `PATCHES/<bug>/patch.diff` plus split rollup sections.
- **frank reference.** `security/scan_archive.py:1-38`, measured on a real
  265-entry archive.
- **Effort.** Small; low value to frank.

---

## 8. Considered and Rejected

Every candidate a verification lens rejected, and every deliberate skip, with
the reason, so nobody re-evaluates it.

| Candidate | Reason |
|-----------|--------|
| Run Magpie's triage rubric as an agent-CLI addendum | frank's agent-cli backend has no addendum wiring, and the rubric's cheat-sheet is Airflow's worked example, not a generic rule. The class criteria were taken instead (4.1). |
| Follow Magpie's SECURITY.md to threat-model chain | frank already auto-discovers `THREAT_MODEL.md`, Magpie's default filename; the chain adds nothing for frank's projects. |
| Read and write a Magpie adopter's private tracker repo | Large; frank's GitHub client has no issue read or write methods; depends on statuses that did not exist yet. Revisit only if an adopter asks. |
| OSV alias lookup (CVE to GHSA) | Nothing in frank's intake produces GHSA ids today; Magpie's own client is "not yet wired". |
| Stop citing the private `security@` thread in the CVE record | Already fixed on this branch (`f59d704`) before the lens ran. |
| Draft reporter acknowledgements into the IMAP Drafts folder | A send-shaped feature with no draft queue behind it; Magpie's version is built on claude.ai mail plumbing frank does not have. |
| Hash-prefixed PII redaction before LLM and CSV egress | frank has no redaction anywhere and the port is large; the distribution names (`redactor`, `checker`) are unsafe to pin as pip deps; a `~/.config` JSON mapping store breaks SQLite-first. Recorded as Tier 2 design input only. |
| PonyMail and Hyperkitty URL recipes | frank's mailing-list fetcher already builds the lists.apache.org URLs correctly; the candidate misread it. |
| Pip-install Magpie's `redactor` and `checker` packages | Generic distribution names that resolve to unrelated PyPI projects if the git spec is ever lost; state in the home directory. The sha-pinned recipes work and are recorded in section 2 for the record. |
| Magpie's mail-source contract, Gmail OAuth tool, PonyMail MCP, mail-patch | One mail backend needs no backend contract; the Gmail tool adds a non-stdlib dependency to solve a claude.ai problem; the PonyMail MCP is unpinnable by its own documentation. |
| `tools/typed-decision` | Depends on a uv-workspace-only `checker` package (a bare pip install would resolve an unrelated PyPI project) and a third-party inference vendor. |
| Security tracker stats dashboard tool | Everything but its date arithmetic assumes `gh` and a tracker repo. |
| Magpie's AI-drafted footer wording (`vendor-ai-attribution-footer-wording`) | Every frank comment already carries an attribution line (`backends/poster.py:28, 105`); Magpie's four maintainer-confirmed footers are written for a different voice and add nothing. |
| The real-CI guard (`adopt-real-ci-guard`) | Rejected on a fact that has since changed: `ci_status` is now written for intending-to-merge and own PRs (`backends/poller.py:770-776`) and gates merge eligibility. Reopened as question 1 in section 9. |
| Deterministic mentor tone lint (`port-deterministic-tone-lint`) | Its rules (no hedging, author tagged exactly once; `tools/pr-management/src/pr_management/mentor/tone.py:33-48`) contradict frank's voice, which hedges on purpose (`personalities/frank.md:27-31`, `review/tone_guard.py:27, 33-34`), and 4.9, which quotes every handle. |
| `git patch-id` parity for the backport check (`port-backport-patch-id-parity`) | frank's backport check is already a deterministic diff-of-diffs over forge diffs (`review/checks/backport.py`); `git patch-id` would need both commits fetched into the worker's default-branch clone, which `_instantiate_check` (`review/checks/__init__.py`) passes to api-misuse only. |
| `<<<MARK ... MARK>>>` prompt delimiters (`agent-cli-prompt-delimiters`) | Done before the lens ran: `core/untrusted.py` fences the diff and prior comments with a nonce in both review paths, and the PR description in the API path (4.4). The title is not fenced, and the agent-CLI prompt carries no title or description. |
| adversarial-review as an extra reviewer (`adversarial-review-extra-reviewer`) | frank already runs several agent CLIs per PR with source attribution and dedup; a second runner whose backends omit cursor-agent, the maintainer's main reviewer, adds a runner, not a view. |
| agent-guard's engine with a frank deny-all guard (`agent-guard-engine-readonly-guard`) | Its console script is bound to the Claude stdin adapter, of frank's four seeded CLIs only claude has a hook adapter, and 4.11's read-only argv now confines every seeded CLI without a hook. |
| A Claude sandbox block via `--settings` (`claude-reviewer-sandbox-settings`) | The block confines sandboxed Bash subprocesses only (`docs/setup/secure-agent-setup.md:616-621`); after 4.11 the claude reviewer has no Bash. |
| egress-gateway as an operator proxy (`egress-gateway-optional-proxy`) | Its allowlist has no Anthropic, OpenAI or Cursor host, so frank's reviewers are blocked until one is added, and then the model host is the exfiltration channel; it also needs `proxy.py`, which is not stdlib. |
| permission-audit and sandbox-lint in CI (`permission-audit-sandbox-lint-ci`) | frank's `.claude/settings.json` has no permissions block, so the audit passes vacuously, and its list is "deliberately not exhaustive". |
| The mentoring tone guide for new contributors (`mentoring-tone-guide-as-new-contributor-addendum`) | Its no-hedging and no-thanks rules contradict `personalities/frank.md:42, 51`, and frank drafts as the maintainer, so "speaking for the maintainer" does not apply. |
| An attribution footer on the auto-poster path (`ai-attribution-footer-on-auto-poster-path`) | False premise: the confidence-gated path already sets its own footer and posts as a separate bot (`review/auto_poster.py:104-122`). |
| A security-signal screen before drafting public comments (`security-signal-screen-before-drafting-public-comments`) | Duplicate of 4.8. |
| A workflow-security check for `.github/workflows/` PRs (`workflow-security-check-for-github-workflows-prs`) | The vendorable text restates zizmor's four audit ids, and frank's checks would scan the default-branch clone, not the PR's workflows; zizmor itself is in 6.8. |
| ASF A/B/X licence policy for added dependencies (`dependency-license-policy-for-added-dependencies`) | The dependency flow persists rows for display and emits no findings, and the detector covers Python only. |
| Flaky-versus-broken CI for shepherding (`flaky-vs-broken-ci-rule-for-shepherding`) | Magpie contributes 20 lines of arithmetic; frank would need Actions run and job history per PR, hundreds of calls on Spark, plus a scrape path. |
| Contributor-health signals in the digest (`contributor-health-signals-in-weekly-digest`) | Community-governance metrics; frank sees only the PRs it ingests, so every cohort would be partial. |
| The mutual-reviews signal via GraphQL (`mutual-reviews-signal-via-contributions-collection-query`) | The query runs through `gh`; frank already has the REST search the design needs (`backends/github.py`, `search_prs_reviewed_by`). |
| Identity grading in collaborator detection (`identity-grading-in-detect-collaborators`) | The wrong fix: `detect_collaborators` guesses a login from each `git log` email's local part, and GitHub's commits API already returns `author.login` for an email linked to an account, which needs no grading. |
| A maintainer-pushback flag for AI-generated PRs (`maintainer-pushback-flag-for-ai-generated-queue`) | Up to 40 prior comments already reach every review prompt (`review/prior_comments.py:27`); a nomination-counting phrase list misfires as a routing signal. |
| The reproducer's verdict vocabulary for the verifier (`reproducer-verdict-vocabulary-and-anti-fabrication-for-sandbox-and-verifier`) | The verifier reads code and never runs it, and already has a fixed affected, not-affected, unclear vocabulary with an anti-guessing rule (`security/verifier.py:103-105, 131`; `parse_verdict` at 645). |
| fix-workflow's golden rules in the fix agent (`fix-workflow-golden-rules-into-fix-agent-instructions`) | "Failing test first" would commit a test demonstrating an undisclosed hole on a public fork branch. |
| Read an adopter's stale-sweep and repo-health config (`read-magpie-adopter-stale-and-repo-health-config`) | Its consumers were rejected, the PR sweep uses different keys from the issue sweep it cites, and `<project-config>` resolves personal-first, so a review checkout rarely holds it. |
| Contribute the injection pre-filter (`contribute-deterministic-injection-prefilter-upstream`) | Magpie deliberately leaves injection detection to the agent (`pre_first_pr/checks.py:17-22`); the sibling proposal survived elsewhere, so 7.6 says to open an issue first. |
| The noreply-login rule for identity-map (`contribute-noreply-login-rule-to-identity-map`) | A misreading: Magpie's commit source starts from the handle, so a noreply address adds nothing. |
| GitLab `/diffs` instead of `/changes` (`gitlab-diffs-endpoint`) | `/changes` works on every GitLab version, and `/diffs` serves the same stored diff under the same limits. |
| A forge-agnostic CI status method (`forge-ci-status-and-mergeable-vocabulary`) | Already exists: `ForgeClient.get_commit_ci_status` (`backends/base.py:148`), implemented for GitHub and written by the poller. |
| Read an adopter's tracker issues (`magpie-tracker-issue-interop`) | The read-only variant of the tracker row above: field names and labels are per adopter, and no frank operator runs a Magpie tracker. |
| Magpie's status-rollup comment format (`status-rollup-comment-format`) | No consumer: frank posts only operator-approved reviews and edits no comments. |
| A Bitbucket Cloud forge client (`bitbucket-cloud-forge-client`) | No operator uses Bitbucket, and Magpie's bridge declares itself partial. |
| Magpie's capability-contract vocabulary for `ForgeClient` (`capability-contract-vocabulary-for-forgeclient`) | The mapping is wrong in places: Magpie's collaborators are people with access, frank's contributors are committers. |
| Mail-archive verbs and the PonyMail timespan grammar (`mail-archive-verbs-and-ponymail-timespan`) | Magpie's side is a contract with no call site, and the grammar belongs to the MCP, not the HTTP API frank calls. |
| ASF roster feeds for committer detection (`asf-roster-feed-for-committer-detection`) | Magpie reads rosters through the apache-projects MCP over the `projects.apache.org/json` feeds, which key on Apache ids and map none to GitHub logins, and the only consumer reads a configured list. |
| The Discord channel-walk pattern (`discord-channel-walk-and-chat-readonly-rule`) | Magpie's adapter wraps a third-party Node MCP and itself uses guild search with a bot token, so the candidate's premise is not Magpie's. |
| JQL search with a host-bound token (`jira-jql-search-and-host-bound-token`) | Sending embargoed report text as JQL to a JIRA server is the egress Magpie's own confidentiality rules forbid. |
| Strip `GIT_*` variables from git subprocesses (`git-env-hygiene-no-vcs-abstraction`) | Guards a tool run inside a git hook; frank's worker is a daemon, and its git gate is a Claude Code PreToolUse hook that runs before git. |
| Finish the lock with an extra, a digest and remote snapshots (`finish-commit-sha-lock-pyproject-extra-and-remote-snapshot-fingerprint`) | The lock is already complete and tested in CI; each "finish" item served pieces that were rejected. |
| A minimal project config to run a Magpie skill headless (`headless-magpie-skill-minimal-project-config`) | Under 4.11 claude and pi have no shell and codex's is a read-only sandbox, so the preflight cannot run, and 4.18 moves `.claude` aside; a skill cannot run in a lane without undoing 4.11, and for claude 4.18 as well. |
| write-skill conventions for personalities (`personality-authoring-conventions-from-write-skill`) | The injection rule already ships (4.4), the bundled personality is about 2,900 characters, far inside the 5,000-token body budget, so the budget changes nothing, and the rest is style. |
| Surface-anchor fingerprints for bumps (`surface-anchor-fingerprint-for-bumps-and-personality-shadowing`) | Solves overrides drifting against upstream headings across many adopters; frank reads vendored files whole and vendors none today. |
| An override audit trail and a no-overrides run (`override-audit-trail-and-no-overrides-run`) | Answers "whose overrides applied" for several people; frank has one operator, and a force-run command exists. |
| A per-project commit-attribution convention (`commit-attribution-convention-per-project`) | Only the Cursor fix agent commits, and it must push an innocuously named branch. |
| The external-content rule in review prompts (`external-content-is-data-in-review-prompts`) | Done as 4.4. |
| Magpie's mode ladder as documentation (`mode-ladder-not-mode-names`) | A naming table that changes no behaviour; its one consequential row is in 6.2. |
| Editorial tone rules in the tone guard (`editorial-tone-rules-into-tone-guard`) | Written for replies to reporters; the tone guard already removes condescension. |
| CVE ids as links (`cve-ids-rendered-as-links`) | Cosmetic; the rule with weight, never mailing the ASF CVE-tool URL to a reporter, has no counterpart because frank mails only the operator (the digest and alerts), never a reporter. |
| AI-contribution norms in REVIEWER.md (`ai-contribution-norms-in-reviewer-md`) | No code loads REVIEWER.md, so the norms would reach only human readers. |
| One standing position per approver (`merge-intent-per-approver-standing-objection`) | Vote-tally's rule is a release-vote majority, not a veto; frank's intended rule is different and needs no port. |
| A moderation flag for committed binaries (`prohibited-binary-files-moderation-flag`) | A seven-entry glob tuple; a binary check belongs in `review/checks/` as a finding and needs no Magpie code. |
| Release lines from an adopter's `release-trains.md` (`release-trains-in-flight-branches-interop`) | Free-form, holds the security roster too, and usually lives in a private tracker repo, not the upstream checkout. |
| The quoted vote-line rule upstream (`contribute-quoted-vote-line-rule-upstream`) | Vote extraction is a model reading the thread, which already separates quoted text, and the proposed eval cannot test it. |
| Fix on the default branch, then backport (`fix-target-default-branch-then-backport`) | Basing on the scanned release branch is deliberate (`security/fix_agent.py:65-67`): a fix based on master fixes the wrong code. |
| A structured JSON recap from the fix agent (`fix-run-structured-recap`) | The pushed commit already carries the files and message, and the operator opens that branch anyway. |
| File-overlap existing-PR discovery upstream (`contribute-file-overlap-existing-pr-discovery`) | An upstream-only proposal whose cost (file lists across every open PR) Magpie's budgeted calls would not take. |
| Run Magpie's code-review, stack-review or self-review skill headless (`skip-magpie-code-review-skill-headless`) | Step 0 needs `gh` and collaborator access on the agent host and every step waits for a person; no executor wiring supplies those, and under 4.11 claude and pi have no shell and codex's is a read-only sandbox, so the preflight cannot run. |
| pr-triage actions, stale sweeps and quick-merge (`skip-triage-stale-quickmerge-mutations`) | Each ends in a PR mutation (draft, close, label, rebase, approve); frank is draft-only. The PR stale sweep's classification is taken in 5.23. |
| The pr-management stats dashboard (`skip-stats-dashboard-and-triage-markers`) | Every panel keys on Magpie triage markers that frank's repositories do not carry. |
| Reviewer routing, typed-decision, mentor hand-off, pre-first-PR check (`skip-reviewer-roster-routing-typed-decision-pre-first-pr`) | frank has one operator and no roster; typed-decision sends contributor text to a third-party endpoint. |
| Upstream frank's tone guard, copypasta or blame (`skip-contribute-tone-copypasta-blame`) | Magpie posts confirmed text verbatim, its tools are almost all stdlib-only, and its `touching-mine` signal covers blame. |
| stack-review (`skip-stack-review`) | Needs GitHub's stacked-PR preview API; frank has no stack concept. |
| container-gateway (`container-gateway-skip`) | frank never hands an agent the Docker socket. In compose local mode a reviewer inside the socket-mounted worker could reach it, so run agent CLIs over ssh or in a sidecar and say so in `compose.yaml`. |
| vetted-ops (`vetted-ops-skip`) | frank posts through the forge API from Python behind a dashboard approval; there is no permission prompt to budget. |
| agent-isolation's interactive helpers (`agent-isolation-interactive-helpers-skip`) | Claude Code session hooks; the worker is headless. |
| contributor-metrics as a pip dependency (`skip-contributor-metrics-as-pip-dependency`) | GitHub access is `gh api graphql`, and `jira-bridge` is an unclaimed PyPI name that only the git spec resolves. |
| Governance, issue-lifecycle and repo-audit skills (`skip-governance-issue-lifecycle-and-audit-skills`) | Workflows frank's roadmap does not have; this skip does not cover the PR stale sweep or `tools/pr-management`. |
| apache-projects and PonyMail MCP wrappers (`ponymail-mcp-adapter-skip`) | Node MCPs on comdev `main`, registered per machine with `claude mcp add`, visible to claude only; PonyMail's private lists need a browser LDAP login on the agent host. |
| SourceHut and Fossil bridges (`sourcehut-fossil-skip`) | Review there is an emailed reply frank cannot draft or recall, and Fossil has no hosted PR surface. |
| ASF SVN and Nexus adapters (`asf-svn-nexus-release-tooling-skip`) | Release-manager workflows; `fix_landed`'s `git tag --contains` is better proof than a dist listing, which keeps only recent releases. |
| "Attacker text never in a shell argument" (`attacker-text-argv-rule-skip`) | Satisfied by construction: argv lists locally and `shlex.quote` on every remote element. That assumes a POSIX login shell on the agent host; one lens would add a one-line convention and a probe. |
| Magpie's safe-redirect handlers (`safe-redirect-handlers-skip`) | httpx strips `Authorization` on cross-origin redirects but not GitLab's `PRIVATE-TOKEN`, so `GitLabClient` must never set `follow_redirects=True`; a test should pin that. |
| Pin Magpie through the marketplace for the worker (`skip-marketplace-plugin-pin-for-worker`) | Tag 0.1.0 has no marketplace manifest, the documented `@0.2.0` does not exist, and a marketplace install is made per machine and per CLI, outside frank's lock. |
| Become a Magpie trusted skill source (`skip-trusted-skill-source-interop`) | RFC-AI-0006 Phase B is still marked in progress, and frank publishes no skills tree. |
| The two-lock drift model and relay symlinks (`skip-two-lock-drift-symlink-relay-model`) | Keeps many machines consistent; frank is one process reading a lock it ships, and `magpie_sync --check` is the verify. |
| skill-token-count (`skip-skill-token-count`) | Needs `tiktoken==0.14.0` and a vocabulary download to measure static files; frank meters live calls. |
| The pilot report template (`skip-pilot-report-template`) | A framework-to-adopter channel; frank has one operator and in-product feedback. |
| Become a Magpie adopter or skill source (`skip-magpie-adopter-or-skill-source`) | A skill needs `python3`, `uv`, `gh` and a working shell in the lane; under 4.11 claude and pi have no shell and codex's is a read-only sandbox. Reopen if a read-only review skill exists after RFC-AI-0006 Phase B. |
| SemBr, doctoc and the no-emoji doc rules (`skip-sembr-and-doc-style`) | They protect prose that is Magpie's executable code; frank's behaviour lives in Python. |
| vote-tally's roster, sender parsing and pass rule (`skip-vote-tally-roster-pass-rule-and-deliberate-divergences`) | Mailing-list vote machinery; frank identifies by forge login. The withdrawal rule is already on this branch (`80080c1`). |
| The release-management lifecycle skills (`skip-release-management-lifecycle-skills`) | Need adopter release config, `gh`, `gpg` and a release manager at the keyboard; frank has no release workflow. |
| pip-installing release-verify or release-config (`skip-pip-release-verify-and-release-config`) | No staged release candidate and no release config in frank. |
| announce-draft, prepare and audit-report security rules (`skip-announce-prepare-audit-security-adjacent-rules`) | Already satisfied: fix branches are scanned for the CVE id and reporter name (4.3), and CVE actions are buttons. |
| Run security-issue-fix inside the fix agent (`skip-security-issue-fix-skill-in-cursor-cloud-agent`) | Needs `gh`, a tracker and typed confirmations, none of which the Cursor VM has; its rules are taken in 5.4, 5.12 and 6.1. |
| issue-fix's Step 10 tracker update (`skip-issue-fix-tracker-update-step10`) | frank has no tracker; the state is `SecurityReport` columns. |
| issue-fix's Step 4 local-clone checks (`skip-issue-fix-local-clone-step4`) | The fix agent runs in a fresh Cursor VM clone; the fork check is 5.12. |
| The fixing-security-issues overview (`skip-fixing-security-issues-overview-doc`) | Its practices are already in the fix prompt and 4.3. |

---

## 9. Open Questions

1. **Should the real-CI guard come back?** `ci_status` is now written for
   intending-to-merge and own PRs (`backends/poller.py:770-776`) and gates merge
   eligibility (`worker/merge_queue.py:85`). `classify_ci`
   (`worker/merge_intent.py:449-476`) counts `action_required` as a failure, but
   it reads any all-success set of runs as `pass`. A contributor PR whose real
   workflows await approval, and which shows only bot checks such as DCO, reads
   green. `docs/security-design.md` H2 already says a CI result must be marked
   untrusted when the PR modifies workflow files, in any feature that gates on CI. If yes, port `real_ci_ran` and `BOT_CHECKS` from
   `tools/pr-management/src/pr_management/ci.py:37, 76-86` as a new Tier 2 entry
   ahead of 5.19.
2. **Telemetry: opt-in, or a recorded divergence?** Section 1 says frank follows
   Magpie's no-telemetry principle. The design doc's section 13 (lines 2179 and
   2195) and `docs/security-design.md` section 4.8 say telemetry is enabled by
   default, though nothing is built. Either the design flips to opt-in, or 6.2,
   6.4 and 6.10 must list telemetry as a divergence.
3. **Should the fix agent apply the scanner's patch or re-derive the fix?** This
   is the operator's workflow, not a technical call. Applying keeps
   reverse-apply detection exact. Re-deriving is Magpie's rule, guards against
   subtly wrong patches, and makes `scan_already_fixed` answer "unclear" more
   often. 6.1 is written for "check, then apply".
4. **Pip dependencies with generic names, or ports?** Section 8 rejected
   `redactor` and `checker` partly for their generic distribution names. 5.5
   (`generate-cve-json`) and 5.10 (`pr-management`) have generic names too.
   Unlike `redactor` and `checker`, which PyPI resolves to unrelated projects,
   they are unclaimed there today, so only the direct git reference in the
   `magpie` extra resolves them and anyone could register the names. If direct references in an optional extra are
   acceptable, both stay pip by sha. If not, 5.5 becomes about 150 lines of
   ported parsing and 5.10 about 500 lines with tests, and both move from small
   or medium effort to medium or large.
5. **Does a local-mode poll cycle leave the clone on the last PR's tree?**
   Inferred from the code while checking 5.7, not reproduced: in local mode
   the poll cycle runs without a workspace lane (only the interactive thread
   sets one), so an agent-CLI review checks the PR head out in the worker's
   own clone, and `_cleanup_review_branch` (`worker/runner.py`) only detaches
   HEAD there. Until the next cycle's `ensure_repo`, later PRs in the same
   cycle would read `security_model_file`, `THREAT_MODEL.md` auto-discovery and
   full-file context from that tree. ssh mode is unaffected. It predates the
   Magpie work, and 5.7 reads criteria with `git show` for this reason; worth
   its own reproduction and fix.

---

## 10. Sources

- Apache Magpie repository: https://github.com/apache/magpie, pinned at
  `dadb315caa8b881fc1d2195bef03223dac25915a` (2026-10-11, dev version
  `0.9.0.dev202610111534`). Only release tag: `0.1.0` at
  `1890a13d0d3a3b0b2a9c83efb7dcdc837a07795d` (2026-07-13).
- Project site: https://magpie.apache.org/
- ASF announcement of Magpie as a Top-Level Project (2026-06-29):
  https://news.apache.org/foundation/entry/the-apache-software-foundation-announces-new-top-level-project-5
- Magpie's own pinning docs: `docs/setup/marketplace-install.md`,
  `docs/quick-start/other-install-methods.md`, `docs/skill-sources/README.md`
  in the repository above.
