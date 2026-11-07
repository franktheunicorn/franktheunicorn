"""Tests for difficulty-tiered model routing (config/model_routing.py)."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from franktheunicorn.config.model_routing import (
    TASK_DEFAULT_TIERS,
    describe_routing,
    mark_tier_disabled,
    reset_disabled_tiers,
    routed_backend,
    routing_warnings,
    tier_for,
    with_routed_first,
)
from franktheunicorn.config.models import LLMBackendConfig, OperatorConfig

ASF = {
    "provider": "openai",
    "model": "gemma4-26b",
    "base_url": "https://llm.apache.org/v1",
    "api_key_env": "LLMAO_KEY",
    "temperature": 0.3,
}


@pytest.fixture(autouse=True)
def _clean_disabled_tiers() -> Iterator[None]:
    reset_disabled_tiers()
    yield
    reset_disabled_tiers()


def _asf_config(**routing: object) -> OperatorConfig:
    return OperatorConfig.model_validate({"llm_backends": [ASF], "model_routing": routing})


class TestTierInheritance:
    def test_model_only_tier_inherits_connection(self) -> None:
        cfg = _asf_config(tiers={"fast": {"model": "qwen3-8b", "temperature": 0.1}})
        fast = cfg.model_routing.tiers["fast"]
        assert fast.provider == "openai"
        assert fast.base_url == "https://llm.apache.org/v1"
        assert fast.api_key_env == "LLMAO_KEY"
        assert fast.model == "qwen3-8b"
        assert fast.temperature == 0.1

    def test_sampling_fields_are_not_inherited(self) -> None:
        base = {**ASF, "max_tokens": 1234, "extra_body": {"x": 1}, "temperature": 0.9}
        cfg = OperatorConfig.model_validate(
            {"llm_backends": [base], "model_routing": {"tiers": {"fast": {"model": "q"}}}}
        )
        fast = cfg.model_routing.tiers["fast"]
        assert fast.max_tokens == LLMBackendConfig().max_tokens
        assert fast.extra_body == {}
        assert fast.temperature == LLMBackendConfig().temperature

    def test_different_provider_inherits_nothing(self) -> None:
        cfg = _asf_config(tiers={"reasoning": {"provider": "claude", "model": "opus"}})
        reasoning = cfg.model_routing.tiers["reasoning"]
        assert reasoning.provider == "claude"
        assert reasoning.base_url == ""
        assert reasoning.api_key_env == ""

    def test_explicit_connection_field_wins(self) -> None:
        cfg = _asf_config(tiers={"fast": {"model": "q", "api_key_env": "OTHER"}})
        assert cfg.model_routing.tiers["fast"].api_key_env == "OTHER"

    def test_legacy_llm_block_is_inherited_from(self) -> None:
        cfg = OperatorConfig.model_validate(
            {"llm": ASF, "model_routing": {"tiers": {"fast": {"model": "q"}}}}
        )
        assert cfg.model_routing.tiers["fast"].base_url == ASF["base_url"]

    def test_tier_names_normalised(self) -> None:
        cfg = _asf_config(tiers={" Fast ": {"model": "q"}}, tasks={"judge": "FAST"})
        assert "fast" in cfg.model_routing.tiers
        assert cfg.model_routing.tasks == {"judge": "fast"}


class TestTierFor:
    def test_defaults(self) -> None:
        cfg = OperatorConfig()
        assert tier_for(cfg, "judge") == "fast"
        assert tier_for(cfg, "security_triage") == "reasoning"
        assert tier_for(cfg, "ci_analysis") == "standard"
        assert tier_for(cfg, "something-new") == "standard"

    def test_check_defaults(self) -> None:
        cfg = OperatorConfig()
        assert tier_for(cfg, "check:pr-description") == "fast"
        assert tier_for(cfg, "check:concurrency") == "standard"

    def test_llm_check_override_moves_unpinned_checks_only(self) -> None:
        cfg = _asf_config(tasks={"llm_check": "reasoning"})
        assert tier_for(cfg, "check:concurrency") == "reasoning"
        # Has its own built-in default, which is more specific than llm_check.
        assert tier_for(cfg, "check:pr-description") == "fast"

    def test_per_check_override_wins(self) -> None:
        cfg = _asf_config(tasks={"llm_check": "reasoning", "check:design": "fast"})
        assert tier_for(cfg, "check:design") == "fast"


class TestRoutedBackend:
    def test_no_tiers_routes_nothing(self) -> None:
        cfg = OperatorConfig.model_validate({"llm_backends": [ASF]})
        for task in TASK_DEFAULT_TIERS:
            assert routed_backend(cfg, task) is None

    def test_none_and_mock_configs_route_nothing(self) -> None:
        assert routed_backend(None, "judge") is None
        assert routed_backend(MagicMock(), "judge") is None

    def test_routes_to_configured_tier(self) -> None:
        cfg = _asf_config(tiers={"fast": {"model": "qwen3-8b"}, "reasoning": {"model": "27b"}})
        judge = routed_backend(cfg, "judge")
        triage = routed_backend(cfg, "security_triage")
        assert judge is not None and judge.model == "qwen3-8b"
        assert triage is not None and triage.model == "27b"
        # Standard tier unset: standard tasks keep their call-site default.
        assert routed_backend(cfg, "ci_analysis") is None

    def test_missing_tier_falls_back_to_standard_tier(self) -> None:
        cfg = _asf_config(tiers={"standard": {"model": "std"}})
        judge = routed_backend(cfg, "judge")
        assert judge is not None and judge.model == "std"

    def test_custom_tier(self) -> None:
        cfg = _asf_config(
            tiers={"long-context": {"model": "big"}}, tasks={"ci_analysis": "long-context"}
        )
        routed = routed_backend(cfg, "ci_analysis")
        assert routed is not None and routed.model == "big"

    def test_disabled_tier_routes_as_unconfigured(self) -> None:
        cfg = _asf_config(tiers={"reasoning": {"model": "27b"}, "standard": {"model": "std"}})
        mark_tier_disabled("reasoning")
        routed = routed_backend(cfg, "security_triage")
        assert routed is not None and routed.model == "std"
        mark_tier_disabled("standard")
        assert routed_backend(cfg, "security_triage") is None

    def test_with_routed_first(self) -> None:
        cfg = _asf_config(tiers={"fast": {"model": "q"}})
        fallbacks = list(cfg.llm_backends)
        assert [c.model for c in with_routed_first(cfg, "judge", fallbacks)] == [
            "q",
            "gemma4-26b",
        ]
        assert with_routed_first(cfg, "ci_analysis", fallbacks) == fallbacks


class TestDiagnostics:
    def test_describe_unconfigured(self) -> None:
        lines = describe_routing(OperatorConfig())
        assert len(lines) == 1 and "no model_routing.tiers" in lines[0]

    def test_describe_configured(self) -> None:
        cfg = _asf_config(tiers={"fast": {"model": "qwen3-8b"}})
        mark_tier_disabled("fast")
        text = "\n".join(describe_routing(cfg))
        assert "Model tier fast: openai/qwen3-8b [DISABLED by preflight]" in text
        assert "judge -> fast: default openai/gemma4-26b" in text

    def test_warnings(self) -> None:
        cfg = _asf_config(
            tiers={"fast": {"model": "q"}},
            tasks={"jugde": "fast", "judge": "turbo", "check:design": "fast"},
        )
        warnings = "\n".join(routing_warnings(cfg))
        assert "unknown task 'jugde'" in warnings
        assert "tier 'turbo'" in warnings
        assert "check:design" not in warnings

    def test_warns_stub_tier_without_base(self) -> None:
        cfg = OperatorConfig.model_validate({"model_routing": {"tiers": {"fast": {"model": "q"}}}})
        assert any("stub backend" in w for w in routing_warnings(cfg))


class TestCallSites:
    """Each consumer puts its routed tier where the old default was."""

    def test_security_triage_candidates(self) -> None:
        from franktheunicorn.security.triage import _triage_backend_configs

        cfg = _asf_config(tiers={"reasoning": {"model": "27b"}})
        models = [c.model for c in _triage_backend_configs(cfg)]
        assert models[:2] == ["27b", "gemma4-26b"]

    def test_security_triage_explicit_override_still_first(self) -> None:
        from franktheunicorn.security.triage import _triage_backend_configs

        cfg = OperatorConfig.model_validate(
            {
                "llm_backends": [ASF],
                "security_triage": {"llm_backend": {**ASF, "model": "mine"}},
                "model_routing": {"tiers": {"reasoning": {"model": "27b"}}},
            }
        )
        assert [c.model for c in _triage_backend_configs(cfg)][:3] == [
            "mine",
            "27b",
            "gemma4-26b",
        ]

    def test_worthiness(self) -> None:
        from franktheunicorn.review.worthiness import worthiness_backend_configs

        cfg = _asf_config(tiers={"fast": {"model": "q"}})
        assert [c.model for c in worthiness_backend_configs(cfg)] == ["q", "gemma4-26b"]

    def test_mergeability(self) -> None:
        from franktheunicorn.review.mergeability import triage_backend_configs

        cfg = _asf_config(tiers={"fast": {"model": "q"}})
        cfg.mergeability_triage.backends = ["llm"]
        assert [c.model for c in triage_backend_configs(cfg)] == ["q", "gemma4-26b"]

    def test_zombie(self) -> None:
        from franktheunicorn.review.zombie import resolve_backend

        cfg = _asf_config(tiers={"reasoning": {"model": "27b"}})
        backend = resolve_backend(cfg)
        assert backend is not None
        assert backend._config.model == "27b"


@pytest.mark.django_db
class TestDrafterRouting:
    def test_judge_and_tone_guard_use_fast_tier(self, db_pr, spark_project_config) -> None:
        from franktheunicorn.review.backends.base import ReviewFinding, ReviewResult
        from franktheunicorn.review.drafter import draft_review

        cfg = _asf_config(tiers={"fast": {"model": "qwen3-8b"}}, tasks={})
        cfg.judge.enabled = True
        finding = ReviewFinding(
            file_path="src/a.py", line_number=1, title="t", body="a real finding body"
        )
        seen: dict[str, str] = {}

        def fake_judge(findings, ctx, backend_config, **kw):  # type: ignore[no-untyped-def]
            seen["judge"] = backend_config.model
            return []

        def fake_tone(findings, ctx, backend_config=None, **kw):  # type: ignore[no-untyped-def]
            seen["tone"] = backend_config.model
            return findings, [False] * len(findings)

        with (
            patch(
                "franktheunicorn.review.drafter._run_single_backend",
                return_value=("openai", ReviewResult(findings=[finding]), False),
            ),
            patch("franktheunicorn.review.judge.judge_findings", side_effect=fake_judge),
            patch(
                "franktheunicorn.review.judge.apply_verdicts",
                side_effect=lambda f, v: (f, list(range(len(f)))),
            ),
            patch("franktheunicorn.review.drafter.apply_tone_guard_batch", side_effect=fake_tone),
        ):
            draft_review(db_pr, spark_project_config, cfg, diff="diff --git a/x b/x\n")
        assert seen == {"judge": "qwen3-8b", "tone": "qwen3-8b"}


@pytest.mark.django_db
class TestCheckRouting:
    def test_each_check_gets_its_routed_backend(self, db_pr) -> None:
        from franktheunicorn.config.models import ProjectConfig
        from franktheunicorn.review.checks import run_enabled_checks

        project = ProjectConfig(owner="apache", repo="spark", llm_checks=["coverage", "security"])
        cfg = _asf_config(
            tiers={"reasoning": {"model": "27b"}}, tasks={"check:security": "reasoning"}
        )
        seen: dict[str, str] = {}

        def fake(check: object, pr: object, diff: str, ctx: object, backend_config):  # type: ignore[no-untyped-def]
            seen[check.__class__.__name__] = backend_config.model
            return []

        with patch("franktheunicorn.review.checks._run_single_check", side_effect=fake):
            run_enabled_checks(db_pr, "diff", project_config=project, operator_config=cfg)
        models = sorted(seen.values())
        # Security routed to reasoning; coverage on llm_check's standard -> default.
        assert models == ["27b", "gemma4-26b"]


class TestBootPreflight:
    def test_failed_probe_disables_tier_and_shares_probes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from franktheunicorn.config.model_routing import is_tier_disabled
        from franktheunicorn.review.backends.preflight import ProbeResult
        from franktheunicorn.worker.runner import _check_model_tiers

        cfg = _asf_config(
            tiers={
                "fast": {"model": "qwen3-8b"},
                "cheap": {"model": "qwen3-8b"},
                "reasoning": {"model": "gone"},
            }
        )

        def probe(bc: LLMBackendConfig) -> ProbeResult:
            if bc.model == "gone":
                return ProbeResult(ok=False, reason="404 unknown model", probed=True)
            return ProbeResult(ok=True, probed=True)

        with (
            patch(
                "franktheunicorn.review.backends.preflight.probe_llm_backend", side_effect=probe
            ) as mocked,
            caplog.at_level("INFO"),
        ):
            _check_model_tiers(cfg)
        assert mocked.call_count == 2  # fast and cheap share one probe
        assert is_tier_disabled("reasoning")
        assert not is_tier_disabled("fast")
        assert "Model tier reasoning (openai/gone): DISABLED" in caplog.text
        assert "security_triage -> reasoning: default openai/gemma4-26b" in caplog.text
