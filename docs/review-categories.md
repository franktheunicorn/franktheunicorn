# Review comment categories

An audit of what review comments on Spark PRs actually say — frank's own draft
findings on one hand, and real reviewer comments on the other — grouped into
categories, with a focused prompt for each and a note on which are better done
as a deterministic rule than an LLM pass.

The point: spend the model where humans spend their attention, and spend a
**rule** (not a token) where the answer is mechanical. Spark already runs
scalafmt/scalastyle, MiMa, and CI, so frank posting what those tools post is
noise the operator rejects one draft at a time.

## Corpus

- **Agent findings:** 44 draft findings across apache/spark (37), spark-upgrade
  (4), fighthealthinsurance (3), generated 2026-10.
- **Human comments:** 222 comments on 32 recent apache/spark PRs (169 inline,
  53 conversation), bots filtered. **Caveat:** recent Spark PRs are now heavily
  reviewed by an AI reviewer ("Omnigent on Databricks", requested by
  committers) whose inline comments follow a distinct **bold-lead + `Nit (P3)` /
  `Non-blocking (P2)`** house style. Those are counted separately from the
  clearly-human conversational comments below, because they inflate the
  "correctness" and "test" buckets well past what unaided humans write.

## What frank already has

`review/checks/` covers several categories: `coverage` (tests), `api_misuse`,
`pr_description`, `issue_link` (JIRA), `backport`, and `security*`. `copypasta`
(duplication) is its own backend. The gaps versus the human corpus —
correctness, concurrency/resource, edge-cases, comment-vs-code accuracy, and
design — are where reviewers spend the most effort; as of this writing they are
**implemented** as the checks `correctness`, `concurrency`, `edge-case`,
`doc-accuracy`, and `design` (enable via `llm_checks:` in project YAML). The
prompts below are those checks' prompts.

---

## Category distribution

### Agent findings (44)

| Category | n | Notes |
|---|---|---|
| F. Duplication (copypasta) | 28 | ~all were cross-module false positives; now gated by directory distance |
| B. Concurrency / state | 3 | FHI stale closure, timer race, `externalRequestInFlight` |
| A. Correctness / logic | 2 | `registerSubqueries` not added to `runningSubqueries`; redundant `waitForSubqueries` |
| E. API / semantic brittleness | 2 | `indexOf` on a signature string; `rebind` assumes `HashPartitioning` |
| F. Simplification | 2 | simplify `isDatasetDataFrame`; `reduce` only matches `_ unionAll _` |
| E. Version / release targeting | 1 | configs versioned 4.4.0 vs upcoming 4.3.0 |
| I. Process | 1 | merge-conflict / rebase-needed |

Agent output was dominated by one broken mechanical check (copypasta) and a
thin tail of genuine correctness/concurrency findings — the opposite of the
human distribution.

### Human reviewer comments (clearly-human subset)

| Category | Weight | Example |
|---|---|---|
| A. Correctness / logic | heavy | "`find` picks only the first matching key…"; "years after 9999 are valid, while both Scaladocs say 0001–9999; one has to change" |
| B. Concurrency / resource / memory | heavy (Spark) | "a spill can leave the reader holding more heap than before"; "this walks the whole queue in `heldCharge()` on every batch" |
| D. Test adequacy / coverage | heavy | "only read precision 7 is exercised end to end"; "these tests only show that *some* input batch was split" |
| G. Comment / doc / description accuracy | heavy | "the javadoc says a spill can come only between two batches, but nothing checks it"; "the PR description understates what this fixes" |
| C. Edge cases / null / NaN / overflow | medium | "what about NaN, does it also need special handling?"; "`getBatchBytes` can still overflow" |
| J. Design / alternative / cross-cutting | medium | "as an alternative to editing `private[spark]` state, copy to a temp file"; "does pushing shuffle below Union affect AQE skew-join?" |
| E. API / compat / version / config | medium | MiMa, legacy-flag interactions, config versioning |
| F. Duplication / dead code / simplification | medium | "drop the loop, the iterations are byte-identical"; "this line has been a no-op since SPARK-12324" |
| H. Style / naming / formatting nit | light | "nit: rename"; spacing — **mostly what scalafmt/scalastyle already enforce** |
| I. Process / workflow | light-medium | LGTM/+1, "resolve the conflicts?", "file a JIRA and reference it", "cc @x", backport description |

---

## Per-category prompts and rules

Prompts follow the `review/checks/` house style (focused "emit when / do NOT",
one–two-sentence body, fixed `category` in the title, JSON `{"findings": [...]}`
with `{schema}`). Each category also states whether part of it is **deterministic**
— doable with a rule instead of the model.

### A. Correctness / logic  *(LLM — the highest-value gap)*

Deterministic? **No** — this is the core judgement a model is for. (Narrow
slices like "unchecked `Option.get` / `.head`" overlap linters, but the general
case is semantic.)

```
You are checking whether this pull request's change is logically correct —
whether it does what the PR says and preserves the invariants of the code it
touches. This is the comment a maintainer makes when the code will produce a
wrong result, miss a case, or break an assumption the rest of the system relies
on.

Emit a finding when:
- The change yields an incorrect result for some reachable input
- It breaks an invariant or contract the surrounding code depends on
- A condition is inverted, off by one, or matches only the first of several cases
- It silently changes behavior the PR did not intend to change

Ground every finding in the diff: name the input or state that triggers it and
the wrong outcome. If you cannot name a concrete case, do not emit it — a
speculative "this might be unsafe" is noise. Do NOT comment on style, tests,
docs, or performance. One or two sentences; no patch.
Set category to "correctness". Return {"findings": [...]} matching {schema}.
```

### B. Concurrency / resource / memory  *(LLM — the second gap, huge on Spark)*

Deterministic? **Rarely.** A few idioms are rule-checkable (a lock acquired and
not released on an exception path, an unclosed `AutoCloseable`), but reasoning
about heap held across batches or spill accounting is not.

```
You are checking this pull request for concurrency and resource-management
problems: memory held longer than intended, leaks, spill/accounting mistakes,
and thread-safety. On a data-processing engine these are where correct-looking
code fails under load.

Emit a finding when:
- Memory, a buffer, or a resource is held across a boundary it was freed before
  (a batch, a row group, a task), or freed twice, or not at all
- Memory accounting tells the manager a different amount than is actually held
- A field read or written from more than one thread has no synchronization, or a
  lock is acquired on a path that can return/throw before releasing it
- A listener or callback can run before the thing it cleans up has stopped

Name the sequence of events that triggers it (which thread, which batch, which
error path). Do NOT emit a generic "could have a race"; if you cannot trace the
interleaving, skip it. One or two sentences; no patch.
Set category to "concurrency". Return {"findings": [...]} matching {schema}.
```

### C. Edge cases / null / NaN / overflow / boundaries  *(LLM, with a deterministic slice)*

Deterministic? **Partly.** Integer-overflow idioms (`a * b` on `int` widened
late), unchecked `.get()/.head`, and missing-NaN in float comparisons are
pattern-matchable and a good candidate for a linter-style pre-pass; whether a
given boundary *matters* is the model's call.

```
You are checking this pull request for unhandled edge cases: null, NaN,
empty/one-element collections, numeric overflow, min/max boundaries, and
special values (epoch, 'today', year 9999).

Emit a finding when:
- A value that can be null/NaN/empty reaches code that assumes it is not
- An arithmetic expression can overflow its type before it is widened or checked
- A boundary (first, last, zero-length, max precision) is reachable and handled
  wrong or not at all
- A special or sentinel value is folded or compared in a way that changes meaning

Name the specific input and what goes wrong. Do NOT list edge cases that the
code or an existing guard already handles. One or two sentences; no patch.
Set category to "edge-case". Return {"findings": [...]} matching {schema}.
```

### D. Test adequacy  *(LLM — already exists as `coverage`)*

Deterministic? **The trigger, yes; the judgement, no.** "Source changed but no
test file in the diff" is a cheap gate that decides whether to *run* this check;
whether the present tests actually reach the change is semantic (already how
`coverage.py` works — keep it, this is the single best-covered gap).

### E. API / compatibility / versioning / config  *(mostly deterministic)*

Deterministic? **Largely.**
- **New config `version()` vs the current dev version** → rule: parse the
  `version("X.Y.Z")` on added `buildConf` entries, compare to the repo's current
  `version.sbt` / pom version; flag a mismatch. (This is the live "4.4.0 vs
  4.3.0" finding — make it a rule, not an LLM guess.)
- **Binary/source compatibility** → MiMa already does this; don't duplicate.
- **Public API surface changes** → rule: an added/removed `public`/`def` in a
  non-`private[spark]` file is worth surfacing deterministically.

LLM only for "this *semantically* breaks a caller's expectation" (→ `api_misuse`,
which exists).

### F. Duplication / dead code / simplification  *(split)*

Deterministic? **Duplication: yes** — `copypasta` backend (now directory-distance
gated). **Dead code / no-op: partly** — an added line that can't execute (after
`return`, a `<script src>` body, a default re-applied) is rule-checkable in the
obvious cases. **"This is now redundant given the other change": LLM.**

```
You are checking whether code this pull request adds is redundant: a block that
duplicates existing logic, a line that has no effect, or a loop/branch whose
iterations are now identical. Do NOT restate the duplication-detector's output;
look for redundancy created by THIS change.

Emit a finding when:
- Added code is made unreachable or a no-op by the surrounding change
- A loop or branch now has identical iterations/arms after the change
- The change leaves two copies of logic that have to be kept in sync

Say what makes it redundant. Do NOT propose extracting a shared helper for code
that is merely similar. One or two sentences; no patch.
Set category to "simplification". Return {"findings": [...]} matching {schema}.
```

### G. Comment / doc / PR-description accuracy  *(LLM — a real gap)*

Deterministic? **No** (it is a prose-vs-code semantic mismatch), except the
narrow case of a comment citing a symbol/line that no longer exists. Note:
`pr_description` covers the PR body; this adds in-code scaladoc/comment drift,
which is one of the largest human buckets.

```
You are checking whether the comments, scaladoc/javadoc, and claims this pull
request adds or leaves in place actually match the code. Reviewers flag this
constantly: a doc that promises a guarantee the code does not enforce, a comment
describing the old behavior, a PR description that overstates the fix.

Emit a finding when:
- A comment or doc states a behavior/guarantee the adjacent code does not provide
- A comment describes code that the change has since altered (stale comment)
- The PR description claims a fix or scope the diff does not deliver

Quote the mismatch: what the text says vs. what the code does. Do NOT ask for
more comments where none are missing; do NOT comment on wording style. One or
two sentences; no patch.
Set category to "doc-accuracy". Return {"findings": [...]} matching {schema}.
```

### H. Style / naming / formatting  *(DETERMINISTIC — frank should NOT emit)*

Deterministic? **Yes, and owned by other tools.** Spark enforces scalafmt,
scalastyle, and Python formatters in CI. A model "nit: rename / spacing / unused
import" is pure noise the operator rejects. **Rule: suppress this category
entirely** (it is already an anti-pattern class; keep it suppressed). The only
naming comment worth making is semantic ("this name says X but it does Y"),
which belongs under G, not here.

### I. Process / workflow  *(DETERMINISTIC)*

Deterministic? **Yes — these are mechanical and frank already does several.**
- Merge conflict → `ensure_conflict_draft` (exists).
- Missing JIRA link / ticket → `issue_link` (exists).
- Backport description alignment → `backport` (exists).
- LGTM / "resolve conflicts" / "cc @x" / "ok to test" → **not review findings**;
  these are approvals, routing, and CI chatter, handled by the merge-intent and
  merge-readiness queues, not by emitting a comment.

No LLM prompt — if a process signal isn't one of the existing rules, it isn't a
draftable finding.

### J. Design / alternative approach / cross-cutting impact  *(LLM — hardest, high value)*

Deterministic? **No.** This is the "have you considered…" / "does this interact
with X subsystem?" comment. Gate it to touched-area experts via project
`review_guidance`, and keep it rare — a wrong design comment is expensive.

```
You are considering whether this pull request's approach has a problem its
author may not have weighed: a simpler or safer alternative, or an interaction
with another part of the system that the diff does not account for.

Emit a finding ONLY when:
- There is a concretely simpler/safer way to achieve the same result, named
- The change interacts with a specific named subsystem (e.g. AQE, the catalog,
  checkpointing) in a way the PR does not address

This is the rarest and most expensive comment to get wrong. If you are not
confident and specific, emit nothing. Pose it as a question a senior reviewer
would ask, naming the subsystem or the alternative. One or two sentences; no patch.
Set category to "design". Return {"findings": [...]} matching {schema}.
```

---

## Summary: where to spend the model vs. a rule

- **Rules (no LLM):** H (style — suppress), I (process — existing checks +
  queues), E-version (config version vs dev version — new rule), F-duplication
  (copypasta backend), the *trigger* for D.
- **LLM, already built:** D (coverage), E-semantic (api_misuse), G-PR-body
  (pr_description), security.
- **LLM gap checks (now implemented):** `correctness` (A), `concurrency` (B),
  `edge-case` (C), `doc-accuracy` (G-in-code), `design` (J) — matching where
  human reviewers on Spark actually spend their attention. Enabled on
  apache/spark; each was verified live to fire on a planted bug and to return
  an empty array on a clean PR.
