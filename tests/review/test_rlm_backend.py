"""Tests for the RLM backend and its registry wiring."""

from __future__ import annotations

from franktheunicorn.config.models import LLMBackendConfig, RLMConfig
from franktheunicorn.review.backends import get_backend
from franktheunicorn.review.backends.base import PRContext
from franktheunicorn.review.rlm.backend import RLMBackend

_DIFF = "+++ b/a.py\n+++ b/b.py\n"


def _ctx() -> PRContext:
    return PRContext(
        pr_title="t",
        pr_body="b",
        pr_author="a",
        pr_number=42,
        project_name="o/r",
        review_context="",
        review_style="",
        tone="",
        test_expectations="",
        governance="standard",
    )


def _rlm_config(leaf_provider: str = "stub") -> LLMBackendConfig:
    return LLMBackendConfig(
        provider="rlm",
        rlm=RLMConfig(leaf=LLMBackendConfig(provider=leaf_provider)),
    )


def test_registry_returns_rlm_backend() -> None:
    backend = get_backend(_rlm_config())
    assert isinstance(backend, RLMBackend)
    # Satisfies the LLMBackend protocol surface.
    assert hasattr(backend, "generate_review")
    assert hasattr(backend, "generate_findings")


def test_generate_review_produces_findings() -> None:
    backend = get_backend(_rlm_config())
    result = backend.generate_review(_DIFF, _ctx())
    assert result.findings
    assert {f.file_path for f in result.findings} <= {"a.py", "b.py"}


def test_generate_findings_wraps_review() -> None:
    backend = get_backend(_rlm_config())
    findings = backend.generate_findings(_DIFF, _ctx())
    assert isinstance(findings, list)


def test_rlm_leaf_provider_falls_back_to_stub() -> None:
    # A leaf configured as 'rlm' would recurse forever; guard demotes to stub.
    backend = RLMBackend(_rlm_config(leaf_provider="rlm"))
    assert backend._leaf_config.provider == "stub"
    # Still produces a result without infinite recursion.
    result = backend.generate_review(_DIFF, _ctx())
    assert isinstance(result.findings, list)
