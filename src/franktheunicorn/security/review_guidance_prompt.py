"""Prompt construction for review-guidance distillation from CVE-introducing commits.

Two calls, mirroring ``security/learning.py``'s distillation shape:
* per-commit — "here is the commit that introduced CVE-X and its diff; what
  should a code reviewer have looked for to catch this at review time?"
* distill — "here are the per-commit notes; distill them into general,
  reusable review guidance."

The per-commit call is bounded: the diff is capped so one large commit
cannot crowd the others out of the prompt, and the notes are short by
design (two or three bullets each).
"""

from __future__ import annotations

from franktheunicorn.core.untrusted import UNTRUSTED_CONTENT_RULE, fence, fence_note, fresh_nonce

#: Cap on the per-commit diff handed to the call, in characters. A real
#: introducing commit can touch many files; the diff that matters for "what
#: should a reviewer have caught" is the hunk that introduced the vulnerable
#: code, not the whole commit's boilerplate.
MAX_COMMIT_DIFF_CHARS = 12_000

#: Marker appended when a commit diff is cut, so the model is told rather than
#: left to infer that a diff stops mid-hunk.
_TRUNCATION_MARKER = "\n\n[commit diff truncated for length]"


def truncate_diff(diff: str, limit: int = MAX_COMMIT_DIFF_CHARS) -> str:
    """Cut *diff* to *limit* characters at a line boundary where possible."""
    if len(diff) <= limit:
        return diff
    cut = diff.rfind("\n", 0, limit)
    if cut <= 0:
        cut = limit
    return diff[:cut] + _TRUNCATION_MARKER


PER_COMMIT_SYSTEM = (
    "You are a security review coach. You are given the commit that introduced a "
    "real CVE and its diff, plus a one-line description of the vulnerability. "
    "Your job is to say what a code reviewer should have looked for in this "
    "diff to catch this class of issue at review time — before it merged.\n\n"
    "Write 2-3 short bullet points of concrete, generalizable review guidance. "
    "Each bullet should name the pattern to watch for, not recap this one "
    "commit. Write guidance that would help a reviewer scanning a future diff "
    "for the same kind of mistake.\n\n"
    "Return ONLY the bullet points, one per line starting with '- '. No "
    "preamble, no markdown fences."
)


def build_per_commit_prompt(
    commit: str,
    cve_description: str,
    diff: str,
    *,
    nonce: str = "",
) -> str:
    """Build the user message for one per-commit guidance call.

    The diff is attacker-influenced input (it is source code from the repo),
    so it is fenced as untrusted data; the CVE description is frank's own
    summary and stays outside the fence.
    """
    nonce = nonce or fresh_nonce()
    return (
        f"Commit {commit[:12]} introduced a real CVE.\n\n"
        f"Vulnerability: {cve_description}\n\n"
        f"What should a code reviewer have looked for in this commit's diff "
        f"to catch this before it merged?\n\n"
        + fence_note(nonce)
        + "\n\n"
        + fence("commit-diff", truncate_diff(diff), nonce)
    )


DISTILL_SYSTEM = (
    "You are distilling per-commit security review notes into concise, "
    "general guidance for future code review. You are given notes from several "
    "commits that each introduced a real CVE, each note saying what a reviewer "
    "should have caught.\n\n"
    "Summarize recurring patterns as a short bulleted addendum covering:\n"
    "- The kinds of changes that tend to introduce vulnerabilities (e.g. "
    "missing input validation, unsafe deserialization, new network surface).\n"
    "- What a reviewer should specifically look for in a diff to catch each "
    "class early.\n"
    "- Any anti-patterns or smells that recur across the commits.\n\n"
    "Be concise and general — write rules that generalize to future diffs, "
    "not a recap of any single commit. De-duplicate overlapping bullets. "
    "Return ONLY the bulleted guidance text, no preamble, no markdown fences.\n\n"
    + UNTRUSTED_CONTENT_RULE
)


def build_distill_prompt(notes: list[str]) -> str:
    """Build the user message for the distillation call from per-commit notes."""
    parts = ["## Per-Commit Review Notes (from commits that introduced real CVEs)\n"]
    for i, note in enumerate(notes, 1):
        parts.append(f"### Commit {i}\n{note.strip()}\n")
    return "\n".join(parts)
