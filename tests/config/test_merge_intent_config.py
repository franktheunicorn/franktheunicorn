"""MergeIntentConfig: approver resolution and defaults."""

from __future__ import annotations

from franktheunicorn.config.models import MergeIntentConfig, OperatorConfig


class TestMergeIntentConfig:
    def test_defaults_on(self) -> None:
        cfg = MergeIntentConfig()
        assert cfg.enabled is True
        assert cfg.track_ci is True
        assert cfg.approvers == []

    def test_operator_is_the_default_approver(self) -> None:
        assert MergeIntentConfig().approvers_for("holdenk") == ["holdenk"]

    def test_no_operator_no_approvers(self) -> None:
        assert MergeIntentConfig().approvers_for("") == []

    def test_configured_approvers_replace_operator(self) -> None:
        cfg = MergeIntentConfig(approvers=["cloud-fan", "dongjoon-hyun"])
        assert cfg.approvers_for("holdenk") == ["cloud-fan", "dongjoon-hyun"]

    def test_approvers_are_normalised(self) -> None:
        cfg = MergeIntentConfig(approvers=[" @holdenk ", "", "  "])
        assert cfg.approvers == ["holdenk"]

    def test_operator_config_carries_it(self) -> None:
        oc = OperatorConfig(
            github_username="holdenk",
            merge_intent={"enabled": False, "approvers": ["a"], "track_ci": False},
        )
        assert oc.merge_intent.enabled is False
        assert oc.merge_intent.approvers == ["a"]
        assert oc.merge_intent.track_ci is False

    def test_operator_config_default(self) -> None:
        assert OperatorConfig().merge_intent == MergeIntentConfig()
