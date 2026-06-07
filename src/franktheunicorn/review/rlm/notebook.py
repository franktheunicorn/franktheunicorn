"""Builds the IPython notebook that *is* the Recursive Language Model.

The notebook is the authentic RLM mechanism: the PR is bound to a ``CONTEXT``
variable, and the model reviews it by **writing Python that runs in the
notebook** — slicing ``CONTEXT``, searching the code, and calling ``llm()``
recursively (the model calling itself) on sub-parts. All model calls and
findings are brokered back to the host over a Unix socket because the notebook
runs in a ``--network=none`` container.

The cell sources are stdlib-only strings (the container does not have
franktheunicorn installed). They're exposed via ``render_*`` helpers so tests
can assert their contents without nbformat or a kernel.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Fixed in-container mount layout (see sandbox_runner).
DEFAULT_SOCKET_PATH = "/rlm/broker.sock"
DEFAULT_INPUT_PATH = "/rlm/input.json"
DEFAULT_REPO_PATH = "/repo"


def render_config_source(socket_path: str, input_path: str, repo_path: str) -> str:
    """Cell 1: wire up the paths the helpers and driver depend on."""
    return (
        "# --- RLM runtime config (injected by the host) ---\n"
        f"_RLM_SOCKET = {socket_path!r}\n"
        f"_RLM_INPUT = {input_path!r}\n"
        f"_RLM_REPO = {repo_path!r}\n"
    )


# Cell 2: stdlib-only helpers. Defines CONTEXT, the broker client, search
# tools, and the model-call/emit helpers the driver and model code rely on.
CLIENT_SOURCE = '''\
# --- RLM helpers (stdlib only; runs inside the sandbox) ---
import json, os, re, socket, fnmatch, subprocess

with open(_RLM_INPUT) as _fh:
    CONTEXT = json.load(_fh)   # {"diff", "pr", "files", "anti_patterns", "tone", ...}


def _rpc(payload):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(180)
    s.connect(_RLM_SOCKET)
    try:
        s.sendall((json.dumps(payload) + "\\n").encode())
        line = s.makefile("rb").readline()
    finally:
        s.close()
    return json.loads(line) if line else {"ok": False, "error": "no response"}


def models():
    """List the model names you can pass to llm(..., model=NAME)."""
    return _rpc({"op": "models"}).get("models", [])


def llm(prompt, model=None, system=""):
    """Call ANY available model. Use this to recurse on sub-parts of CONTEXT."""
    return _rpc({"op": "llm", "model": model, "prompt": prompt, "system": system}).get("text", "")


def emit_finding(file_path, body, line=None, severity="nit", confidence=0.6, suggestion="", title=""):
    """Record a review finding for the operator."""
    finding = {"file_path": file_path, "line_number": line, "body": body,
               "severity": severity, "confidence": confidence,
               "suggestion": suggestion, "title": title}
    return _rpc({"op": "emit", "finding": finding}).get("ok", False)


def log(message):
    """Send a progress note back to the host worker log."""
    return _rpc({"op": "log", "message": str(message)}).get("ok", False)


# ---- search tools (use these to navigate; do not dump the whole PR) ----

def _corpus(path=None):
    """name -> text mapping over the diff and changed files."""
    files = CONTEXT.get("files", {})
    if path is not None:
        return {path: files[path]} if path in files else {}
    corpus = {"<diff>": CONTEXT.get("diff", "")}
    corpus.update(files)
    return corpus


def list_context():
    """Summarize what is in CONTEXT (keys, file list, sizes)."""
    files = CONTEXT.get("files", {})
    return {"keys": sorted(CONTEXT.keys()),
            "pr": CONTEXT.get("pr", {}),
            "files": {p: len(c) for p, c in files.items()},
            "diff_chars": len(CONTEXT.get("diff", ""))}


def find_files(pattern="*"):
    """Glob the changed-file paths. Falls back to the diff's '+++ b/<path>'
    headers when CONTEXT['files'] is empty (the common notebook-mode case)."""
    paths = list(CONTEXT.get("files", {}))
    if not paths:
        paths = [p.strip() for p in re.findall(r"^\\+\\+\\+ b/(.+)$", CONTEXT.get("diff", ""), re.M)]
    return sorted(p for p in paths if fnmatch.fnmatch(p, pattern))


def read_file(path, max_chars=20000):
    """Read a changed file from CONTEXT, falling back to the read-only repo."""
    files = CONTEXT.get("files", {})
    if path in files:
        return files[path][:max_chars]
    full = os.path.join(_RLM_REPO, path)
    if os.path.isfile(full):
        with open(full, errors="replace") as fh:
            return fh.read(max_chars)
    return ""


def grep(pattern, path=None, ignore_case=True, max_hits=100):
    """Regex-search the diff + files; returns 'name:lineno: line' hits."""
    flags = re.IGNORECASE if ignore_case else 0
    rx = re.compile(pattern, flags)
    hits = []
    for name, text in _corpus(path).items():
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append("%s:%d: %s" % (name, i, line.strip()[:200]))
                if len(hits) >= max_hits:
                    return hits
    return hits


def search(query, max_hits=50):
    """Case-insensitive substring search across the diff + files."""
    return grep(re.escape(query), ignore_case=True, max_hits=max_hits)


def ripgrep(pattern, path=None, max_hits=100):
    """Run ripgrep over the read-only repo when available; else fall back to grep()."""
    root = os.path.join(_RLM_REPO, path) if path else _RLM_REPO
    if os.path.isdir(_RLM_REPO):
        try:
            out = subprocess.run(["rg", "--no-heading", "-n", "-m", str(max_hits), pattern, root],
                                 capture_output=True, text=True, timeout=30)
            if out.returncode in (0, 1):
                return [ln for ln in out.stdout.splitlines() if ln][:max_hits]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return grep(pattern, path=path, max_hits=max_hits)
'''


# Cell 3: the recursive driver. The host model is asked to WRITE PYTHON that
# reviews the PR; that code is exec'd in this namespace and may itself call
# llm()/emit_finding()/the search tools — i.e. the model calling itself.
SYSTEM_PROMPT = """You are a senior code reviewer running INSIDE an IPython notebook.

You do not answer in prose — you REVIEW BY WRITING PYTHON CODE that runs in the
notebook. The PR under review is already loaded into the variable CONTEXT
(a dict with keys: diff, pr, files, anti_patterns, tone).

Tools already defined in the namespace:
- models()                      -> list of model names you may call
- llm(prompt, model=None)       -> call ANY of those models; use it to RECURSE
                                   on sub-parts (e.g. one call per file/hunk).
                                   This is how you call yourself on smaller pieces.
- search tools: grep(pattern), search(query), find_files(glob), read_file(path),
  ripgrep(pattern), list_context()  -> USE THESE to navigate; never try to read
                                       the whole PR at once.
- emit_finding(file_path, body, line=, severity=, confidence=, suggestion=)
                                -> record each issue you find.

Strategy: decompose. Use the search tools to locate risk, split large files by
calling llm() recursively on each chunk, then emit_finding() for every concrete
issue. Avoid anti_patterns. When you are completely done, set RLM_DONE = True.

Reply with EXACTLY ONE ```python code block and nothing else."""

DRIVER_SOURCE = """\
# --- RLM recursive driver ---
import re as _re

RLM_DONE = False
_MAX_STEPS = 8


def _extract_code(text):
    m = _re.search(r"```(?:python)?\\s*\\n(.*?)```", text, _re.DOTALL)
    return (m.group(1) if m else text).strip()


def _step_prompt(history):
    summary = list_context()
    return ("Available models: %s\\n"
            "CONTEXT summary: %s\\n"
            "History so far: %s\\n"
            "Write the next python step. Set RLM_DONE = True when finished."
            % (models(), json.dumps(summary)[:4000], history[-5:]))


def run_review(max_steps=_MAX_STEPS):
    history = []
    for step in range(max_steps):
        reply = llm(SYSTEM_PROMPT + "\\n\\n" + _step_prompt(history))
        code = _extract_code(reply)
        if not code:
            history.append("step %d: empty reply, stopping" % step)
            break
        try:
            exec(compile(code, "<rlm-step-%d>" % step, "exec"), globals())
            history.append("step %d: ok" % step)
        except Exception as exc:  # surface errors back to the model next round
            history.append("step %d: error %r" % (step, exc))
            log("rlm step %d error: %r" % (step, exc))
        if globals().get("RLM_DONE"):
            break
    log("rlm finished after %d step(s)" % (step + 1))


run_review()
"""


def render_driver_source() -> str:
    """Cell 3 source with the system prompt baked in as a literal assignment."""
    return f"SYSTEM_PROMPT = {SYSTEM_PROMPT!r}\n\n{DRIVER_SOURCE}"


def build_input_payload(
    diff: str,
    *,
    pr: dict[str, Any],
    files: dict[str, str] | None = None,
    anti_patterns: list[str] | None = None,
    tone: str = "",
) -> dict[str, Any]:
    """Assemble the JSON payload bound to CONTEXT inside the notebook."""
    return {
        "diff": diff,
        "pr": pr,
        "files": files or {},
        "anti_patterns": anti_patterns or [],
        "tone": tone,
    }


def _code_cell(source: str) -> dict[str, Any]:
    return {
        "cell_type": "code",
        "metadata": {},
        "execution_count": None,
        "outputs": [],
        "source": source,
    }


def build_notebook_json(
    *,
    socket_path: str = DEFAULT_SOCKET_PATH,
    input_path: str = DEFAULT_INPUT_PATH,
    repo_path: str = DEFAULT_REPO_PATH,
) -> dict[str, Any]:
    """Build the executable RLM notebook as an ``.ipynb`` v4 dict.

    Built as plain JSON so the *host* needs no Jupyter dependency — only the
    container image that runs ``jupyter execute`` needs nbclient/ipykernel.
    """
    return {
        "cells": [
            _code_cell(render_config_source(socket_path, input_path, repo_path)),
            _code_cell(CLIENT_SOURCE),
            _code_cell(render_driver_source()),
        ],
        "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def write_notebook(path: str, **kwargs: str) -> None:
    """Write the RLM notebook to ``path`` as ipynb JSON (no Jupyter needed)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(build_notebook_json(**kwargs), fh)


def parse_finding(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw emitted finding dict into ReviewFinding kwargs."""
    return {
        "file_path": str(raw.get("file_path", "")),
        "line_number": raw.get("line_number"),
        "title": str(raw.get("title", "")),
        "body": str(raw.get("body", "")),
        "suggestion": str(raw.get("suggestion", "")),
        "severity": str(raw.get("severity", "nit")),
    }
