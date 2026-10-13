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

Every candidate a verification lens rejected, with the reason, so nobody
re-evaluates it. (Rows are added as areas finish verifying.)

| Candidate | Reason |
|-----------|--------|
| Run Magpie's triage rubric as an agent-CLI addendum | frank's agent-cli backend has no addendum wiring, and the rubric's cheat-sheet is Airflow's worked example, not a generic rule. The class criteria were taken instead (4.1). |
| Follow Magpie's SECURITY.md to threat-model chain | frank already auto-discovers `THREAT_MODEL.md`, Magpie's default filename; the chain adds nothing for frank's projects. |
| Read and write a Magpie adopter's private tracker repo | Large; frank's GitHub client has no issue read or write methods; depends on statuses that did not exist yet. Revisit only if an adopter asks. |
| OSV alias lookup (CVE to GHSA) | Nothing in frank's intake produces GHSA ids today; Magpie's own client is "not yet wired". |
| Stop citing the private `security@` thread in the CVE record | Already fixed on this branch (`a57191e`) before the lens ran. |
| Draft reporter acknowledgements into the IMAP Drafts folder | A send-shaped feature with no draft queue behind it; Magpie's version is built on claude.ai mail plumbing frank does not have. |
| Hash-prefixed PII redaction before LLM and CSV egress | frank has no redaction anywhere and the port is large; the distribution names (`redactor`, `checker`) are unsafe to pin as pip deps; a `~/.config` JSON mapping store breaks SQLite-first. Recorded as Tier 2 design input only. |
| PonyMail and Hyperkitty URL recipes | frank's mailing-list fetcher already builds the lists.apache.org URLs correctly; the candidate misread it. |
| Pip-install Magpie's `redactor` and `checker` packages | Generic distribution names that resolve to unrelated PyPI projects if the git spec is ever lost; state in the home directory. The sha-pinned recipes work and are recorded in section 2 for the record. |
| Magpie's mail-source contract, Gmail OAuth tool, PonyMail MCP, mail-patch | One mail backend needs no backend contract; the Gmail tool adds a non-stdlib dependency to solve a claude.ai problem; the PonyMail MCP is unpinnable by its own documentation. |
| `tools/typed-decision` | Depends on a uv-workspace-only `checker` package (a bare pip install would resolve an unrelated PyPI project) and a third-party inference vendor. |
| Security tracker stats dashboard tool | Everything but its date arithmetic assumes `gh` and a tracker repo. |

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
