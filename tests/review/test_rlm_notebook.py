"""Tests for the RLM notebook builder + sources."""

from __future__ import annotations

import json
from pathlib import Path

from franktheunicorn.review.rlm.notebook import (
    CLIENT_SOURCE,
    SYSTEM_PROMPT,
    build_input_payload,
    build_notebook_json,
    parse_finding,
    render_config_source,
    render_driver_source,
    write_notebook,
)


def test_config_source_injects_paths() -> None:
    src = render_config_source("/rlm/broker.sock", "/rlm/input.json", "/repo")
    assert "_RLM_SOCKET = '/rlm/broker.sock'" in src
    assert "_RLM_INPUT = '/rlm/input.json'" in src
    assert "_RLM_REPO = '/repo'" in src


def test_client_source_binds_context_and_search_tools() -> None:
    # The prompt is bound to a variable, RLM-style.
    assert "CONTEXT = json.load" in CLIENT_SOURCE
    # All the search tools the model is told about must exist.
    for tool in (
        "def grep(",
        "def search(",
        "def find_files(",
        "def read_file(",
        "def ripgrep(",
        "def list_context(",
    ):
        assert tool in CLIENT_SOURCE
    # It can call any model, and emit findings.
    assert "def models(" in CLIENT_SOURCE
    assert "def llm(" in CLIENT_SOURCE
    assert "def emit_finding(" in CLIENT_SOURCE


def test_system_prompt_makes_model_aware_of_capabilities() -> None:
    # Model is told it can call any model, recurse, and use search tools.
    assert "models()" in SYSTEM_PROMPT
    assert "llm(prompt" in SYSTEM_PROMPT
    assert "call yourself" in SYSTEM_PROMPT
    assert "search tools" in SYSTEM_PROMPT
    assert "grep(pattern)" in SYSTEM_PROMPT
    assert "RLM_DONE" in SYSTEM_PROMPT


def test_driver_source_runs_recursive_loop() -> None:
    driver = render_driver_source()
    assert "def run_review(" in driver
    assert "run_review()" in driver
    # The system prompt is baked into the executable driver.
    assert "senior code reviewer" in driver


def test_build_notebook_json_structure() -> None:
    nb = build_notebook_json()
    assert nb["nbformat"] == 4
    assert len(nb["cells"]) == 3
    assert all(c["cell_type"] == "code" for c in nb["cells"])


def test_write_notebook_is_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "review.ipynb"
    write_notebook(str(path))
    loaded = json.loads(path.read_text())
    assert loaded["nbformat"] == 4
    assert len(loaded["cells"]) == 3


def test_build_input_payload() -> None:
    payload = build_input_payload(
        "diff text",
        pr={"number": 7},
        files={"a.py": "code"},
        anti_patterns=["no nits"],
        tone="kind",
    )
    assert payload["diff"] == "diff text"
    assert payload["files"] == {"a.py": "code"}
    assert payload["anti_patterns"] == ["no nits"]


def test_parse_finding_normalizes() -> None:
    out = parse_finding({"file_path": "a.py", "body": "x", "severity": "important"})
    assert out == {
        "file_path": "a.py",
        "line_number": None,
        "title": "",
        "body": "x",
        "suggestion": "",
        "severity": "important",
    }
