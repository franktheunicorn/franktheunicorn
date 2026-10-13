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
Autonomous); only Triage and Drafting are stable, and Autonomous is off by
design. Its state lives in the forge: a private tracker repository's issues,
labels and a status rollup comment. Its safety posture is the part frank should
take most seriously: external content is data and never an instruction; outputs
are probabilistic and gates are deterministic; every agent-authored output is a
proposal a human signs; no telemetry; a committed lock plus gitignored snapshot
instead of vendored copies.

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
| Agent confinement | per-CLI read-only argv, credential-stripped env, PreToolUse guard, sandbox settings | containers for tests and PoC runs, `--mode ask` for cursor-agent, nonce-framed prompts in the verifier and fix agent |
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
no telemetry (local-first), audit of every posted action (operator actions and
the 24h recall window). Principles frank does not follow and should adopt:
external content is data in every prompt that carries contributor or reporter
text (today only the verifier and the fix agent frame it), and tracker
identifiers are public-safe but contents are not (the CVE record must never
cite a private thread). One principle frank cannot adopt as written:
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
- **Vendored text** lives in `src/franktheunicorn/magpie/vendored/`. Each file
  opens with a provenance header naming the upstream path and commit and keeps
  Magpie's own SPDX line in its body. `vendored_text()` in
  `src/franktheunicorn/magpie/pin.py` returns the body without the header, so
  prompts never carry the comment.
- **Worker-side Python tools** are installed by `pip` from the git URL at the
  pinned sha plus `#subdirectory=tools/<name>`. `PinnedTool.requirement()`
  renders the exact string. They live in an optional dependency group so a
  default install pulls nothing from GitHub.
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

The maintainer's deployment runs the agent CLIs (`cursor-agent`, `claude`,
`codex`, `pi`) on another host over SSH. frank holds no API tokens for those
models; auth is the CLI's own login on that host. Some SSH wrappers have no
remote-command form at all, so frank drives them by piping a framed script into
the interactive shell they open. The executor is
`src/franktheunicorn/review/tool_executor.py` and the config is
`RemoteExecutionConfig` in `src/franktheunicorn/config/models.py`. Every
Magpie piece has to be placed against that model.

Three places a piece can execute:

| Location | What runs there | Reaches it how |
|----------|-----------------|----------------|
| Worker host | frank's Python: prompt building, parsing, triage, dedup, lock checks | ordinary imports; pinned tools via `pip` |
| Remote agent host | the agent CLI inside a checkout under `remote_workspace_dir` | one shell invocation per call through `executor.run()` or `run_script()`; a process stdin payload is dropped in stdin delivery mode, output is capped at 4 MB |
| The model | prompt text | argv; the staging base64s the whole line, so size is not a constraint |

What fits that model, verified by running each on this box:

1. **Prompt text** vendored into frank's templates needs nothing anywhere and
   works with every CLI and every wrapper. This is the default for everything
   that is a rule, a rubric or a checklist.
2. **Worker-side `pip` by sha** for stdlib-only tools frank calls before a
   prompt leaves the worker (for example the PII redactor's functions).
3. **`uvx` from the pinned sha** on the agent host, for a tool that must run
   next to the agent. Only `uv` has to exist there; the first run builds and
   caches, later runs are instant. `uvx --from "git+https://github.com/apache/magpie.git@dadb315caa8b881fc1d2195bef03223dac25915a#subdirectory=tools/agent-guard" agent-guard`
   was run here end to end.
4. **A pinned snapshot on the agent host** when a tool must be a file path (a
   hook) or when `uv` is absent: `git init`, `remote add`,
   `fetch --depth 1 --filter=blob:none origin <sha>`, `checkout FETCH_HEAD`
   fetches in about a second (61 MB full, 6 MB with a sparse checkout of the
   four directories frank reads). Magpie's stdlib tools then run with plain
   `python3` from the snapshot; `agent-guard` and the PII redactor both did
   here. Skills become visible to a CLI by symlinking
   `<snapshot>/plugins/<family>/skills/<skill>` to `.agents/skills/magpie-<skill>`
   (read by cursor-agent, codex, gemini, copilot) and `.claude/skills/magpie-<skill>`
   (claude) inside the checkout. Lane worktrees share no working tree, so the
   links are made per lane.

What does not fit:

- Claude Code marketplace plugins (`/plugin marketplace add apache/magpie@0.1.0`)
  are per-machine, interactive, and never reach cursor-agent or codex. They are
  fine on a laptop and irrelevant to the worker.
- Any skill whose body shells out to `gh` on the agent host. frank does not
  assume `gh` there, and the review checkout is not a Magpie adopter repo, so
  the skill's preflight would fail before the work started.
- `PreToolUse` hooks apply to `claude` sessions only; `cursor-agent` has no hook
  Magpie supports.

The degradation rule is the one in CLAUDE.md: a configured Magpie piece that
cannot run on the remote (no `uv`, no `python3`, a failed fetch) logs once at
WARNING naming the host and the setting, and the review proceeds without it.

_Per-piece table to be filled from the verified mapping._

---

## 4. Tier 1: Adopt Now

_To be filled._

---

## 5. Tier 2: Adopt Later

_To be filled._

---

## 6. Tier 3: Borrow the Text or the Design

_To be filled._

---

## 7. Interop and Contributions Upstream

_To be filled._

---

## 8. Considered and Rejected

_To be filled._

---

## 9. Open Questions

_To be filled._

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
