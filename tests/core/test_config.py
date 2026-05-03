"""Tests for YAML config loading, validation, and Pydantic models."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from franktheunicorn.config.loader import (
    get_operator_config,
    get_project_config,
    load_operator_config,
    load_project_configs,
)
from franktheunicorn.config.models import OperatorConfig, ProjectConfig
from franktheunicorn.config.schema import validate_yaml_file


class TestOperatorConfig:
    def test_defaults(self) -> None:
        config = OperatorConfig()
        assert config.github_username == ""
        assert config.auto_post is False
        assert config.poll_interval_seconds is None

    def test_from_values(self) -> None:
        config = OperatorConfig(
            github_username="holdenk",
            review_style="direct but kind",
        )
        assert config.github_username == "holdenk"
        assert config.review_style == "direct but kind"

    def test_empty_llm_backends_default(self) -> None:
        config = OperatorConfig()
        assert config.llm_backends == []

    def test_multiple_llm_backends(self) -> None:
        from franktheunicorn.config.models import LLMBackendConfig

        config = OperatorConfig(
            llm_backends=[
                LLMBackendConfig(provider="claude", model="claude-sonnet-4-20250514"),
                LLMBackendConfig(provider="ollama", model="qwen2.5-coder:14b"),
            ],
        )
        assert len(config.llm_backends) == 2
        assert config.llm_backends[0].provider == "claude"
        assert config.llm_backends[1].provider == "ollama"

    def test_legacy_llm_field_promoted_to_backends(self) -> None:
        from franktheunicorn.config.models import LLMBackendConfig

        config = OperatorConfig(llm=LLMBackendConfig(provider="openai"))
        assert len(config.llm_backends) == 1
        assert config.llm_backends[0].provider == "openai"

    def test_legacy_llm_ignored_when_backends_set(self) -> None:
        from franktheunicorn.config.models import LLMBackendConfig

        config = OperatorConfig(
            llm=LLMBackendConfig(provider="openai"),
            llm_backends=[LLMBackendConfig(provider="claude")],
        )
        # llm_backends takes precedence; legacy field not promoted.
        assert len(config.llm_backends) == 1
        assert config.llm_backends[0].provider == "claude"


class TestAgentFeedbackConfig:
    def test_defaults(self) -> None:
        config = OperatorConfig()
        assert config.agent_feedback.direct_session_enabled is True
        assert config.agent_feedback.supported_agents == []

    def test_with_supported_agents(self) -> None:
        from franktheunicorn.config.models import AgentFeedbackConfig, SupportedAgentConfig

        config = OperatorConfig(
            agent_feedback=AgentFeedbackConfig(
                direct_session_enabled=True,
                supported_agents=[
                    SupportedAgentConfig(
                        name="claude-code",
                        session_pattern=r"Session:\s*(https://claude\.ai/code/session/\S+)",
                        feedback_method="url-open",
                    ),
                    SupportedAgentConfig(
                        name="codex",
                        session_pattern=r"Task ID:\s*(task_\S+)",
                        feedback_method="api",
                        api_endpoint_env="CODEX_FEEDBACK_API",
                    ),
                ],
            ),
        )
        assert len(config.agent_feedback.supported_agents) == 2
        assert config.agent_feedback.supported_agents[0].name == "claude-code"
        assert config.agent_feedback.supported_agents[1].feedback_method == "api"

    def test_disabled(self) -> None:
        from franktheunicorn.config.models import AgentFeedbackConfig

        config = OperatorConfig(
            agent_feedback=AgentFeedbackConfig(direct_session_enabled=False),
        )
        assert config.agent_feedback.direct_session_enabled is False

    def test_from_dict(self) -> None:
        config = OperatorConfig(
            **{
                "agent_feedback": {
                    "direct_session_enabled": True,
                    "supported_agents": [
                        {"name": "claude-code", "feedback_method": "url-open"},
                    ],
                },
            }
        )
        assert len(config.agent_feedback.supported_agents) == 1


class TestEmailConfig:
    def test_defaults(self) -> None:
        from franktheunicorn.config.models import EmailConfig

        config = EmailConfig()
        assert config.smtp_host == ""
        assert config.smtp_port == 587
        assert config.smtp_user == ""
        assert config.smtp_pass == ""
        assert config.from_address == "frank@localhost"
        assert config.use_tls is True

    def test_configured(self) -> None:
        from franktheunicorn.config.models import EmailConfig

        config = EmailConfig(
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_user="user@example.com",
            smtp_pass="secret",
            from_address="frank@example.com",
            use_tls=False,
        )
        assert config.smtp_host == "smtp.example.com"
        assert config.smtp_port == 465
        assert config.from_address == "frank@example.com"

    def test_invalid_port_rejected(self) -> None:
        from franktheunicorn.config.models import EmailConfig

        with pytest.raises(ValidationError, match="smtp_port"):
            EmailConfig(smtp_port=0)


class TestOperatorConfigUnifiedFields:
    def test_unified_defaults(self) -> None:
        config = OperatorConfig()
        assert config.mock_mode is False
        assert config.data_dir == ""
        assert config.fixtures_dir == ""
        assert config.repos_dir == ""
        assert config.projects_dir == ""
        assert config.github_token == ""
        assert config.email.smtp_host == ""

    def test_mock_mode(self) -> None:
        config = OperatorConfig(mock_mode=True)
        assert config.mock_mode is True

    def test_github_token(self) -> None:
        config = OperatorConfig(github_token="ghp_test123")
        assert config.github_token == "ghp_test123"

    def test_data_dir(self) -> None:
        config = OperatorConfig(data_dir="/custom/data")
        assert config.data_dir == "/custom/data"

    def test_email_from_dict(self) -> None:
        config = OperatorConfig(
            email={
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "from_address": "frank@example.com",
            },
        )
        assert config.email.smtp_host == "smtp.example.com"
        assert config.email.smtp_port == 465


class TestProjectConfig:
    def test_defaults(self) -> None:
        config = ProjectConfig(owner="apache", repo="spark")
        assert config.full_name == "apache/spark"
        assert config.enabled is True
        assert config.watched_paths == []

    def test_full_config(self) -> None:
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            watched_paths=["sql/catalyst/"],
            frequent_contributors=["cloud-fan"],
            governance="asf",
        )
        assert config.full_name == "apache/spark"
        assert "sql/catalyst/" in config.watched_paths
        assert config.governance == "asf"


class TestOperatorConfigValidation:
    @pytest.mark.parametrize("value", [-1, 0], ids=["negative", "zero"])
    def test_non_positive_poll_interval_rejected(self, value: int) -> None:
        with pytest.raises(ValidationError, match="poll_interval_seconds must be positive"):
            OperatorConfig(poll_interval_seconds=value)

    def test_positive_poll_interval_accepted(self) -> None:
        config = OperatorConfig(poll_interval_seconds=60)
        assert config.poll_interval_seconds == 60

    def test_none_poll_interval_accepted(self) -> None:
        config = OperatorConfig()
        assert config.poll_interval_seconds is None

    def test_invalid_github_username_rejected(self) -> None:
        with pytest.raises(ValidationError, match="invalid characters"):
            OperatorConfig(github_username="has spaces")

    def test_empty_github_username_accepted(self) -> None:
        config = OperatorConfig(github_username="")
        assert config.github_username == ""

    def test_whitespace_username_stripped(self) -> None:
        config = OperatorConfig(github_username="  holdenk  ")
        assert config.github_username == "holdenk"


class TestProjectConfigValidation:
    def test_empty_owner_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            ProjectConfig(owner="", repo="spark")

    def test_empty_repo_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            ProjectConfig(owner="apache", repo="")

    def test_invalid_owner_pattern_rejected(self) -> None:
        with pytest.raises(ValidationError, match="invalid characters"):
            ProjectConfig(owner="has spaces", repo="spark")

    def test_unknown_governance_accepted_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            config = ProjectConfig(owner="x", repo="y", governance="Unknown")
        assert config.governance == "unknown"  # lowercased
        assert "Unknown governance value" in caplog.text

    def test_known_governance_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            ProjectConfig(owner="x", repo="y", governance="ASF")
        assert "Unknown governance value" not in caplog.text

    def test_overlapping_paths_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            ProjectConfig(
                owner="x",
                repo="y",
                watched_paths=["src/", "docs/"],
                ignore_paths=["docs/"],
            )
        assert "watched_paths and ignore_paths" in caplog.text
        assert "docs/" in caplog.text

    def test_no_overlap_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING):
            ProjectConfig(
                owner="x",
                repo="y",
                watched_paths=["src/"],
                ignore_paths=["docs/"],
            )
        assert "watched_paths and ignore_paths" not in caplog.text


class TestConfigLoader:
    def test_load_operator_config_from_file(self, tmp_config_dir: Path) -> None:
        config = load_operator_config(tmp_config_dir / "operator.yaml")
        assert config.github_username == "testuser"
        assert config.poll_interval_seconds == 60

    def test_load_operator_config_missing_file(self, tmp_path: Path) -> None:
        config = load_operator_config(tmp_path / "nonexistent.yaml")
        assert config.github_username == ""  # defaults

    def test_load_project_configs(self, tmp_config_dir: Path) -> None:
        configs = load_project_configs(tmp_config_dir / "projects")
        assert len(configs) == 1
        assert configs[0].owner == "testorg"
        assert configs[0].repo == "testrepo"
        assert "src/" in configs[0].watched_paths

    def test_load_project_configs_missing_dir(self, tmp_path: Path) -> None:
        configs = load_project_configs(tmp_path / "nonexistent")
        assert configs == []

    def test_load_project_configs_normalizes_legacy_merge_queue_keys(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        config_file = projects_dir / "legacy.yaml"
        config_file.write_text(
            "\n".join(
                [
                    "owner: apache",
                    "repo: spark",
                    "merge_queue:",
                    "  enabled: true",
                    "  restack: true",
                    "  stale_migration_strategy: none",
                ]
            )
        )

        configs = load_project_configs(projects_dir)
        assert len(configs) == 1
        assert configs[0].merge_queue.restack_enabled is True
        assert configs[0].merge_queue.delete_stale_migrations is False


class TestYAMLErrorHandling:
    def test_load_operator_config_invalid_yaml(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text(": invalid: yaml: [")
        config = load_operator_config(bad_file)
        assert config.github_username == ""  # fell back to defaults

    def test_load_operator_config_validation_error(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("poll_interval_seconds: -5\n")
        config = load_operator_config(bad_file)
        assert config.poll_interval_seconds is None  # fell back to defaults

    def test_load_project_configs_skips_invalid(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        good = projects_dir / "good.yaml"
        good.write_text("owner: apache\nrepo: spark\n")

        bad = projects_dir / "bad.yaml"
        bad.write_text(": invalid: yaml: [")

        configs = load_project_configs(projects_dir)
        assert len(configs) == 1
        assert configs[0].owner == "apache"

    def test_load_project_configs_skips_validation_error(self, tmp_path: Path) -> None:
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        good = projects_dir / "good.yaml"
        good.write_text("owner: apache\nrepo: spark\n")

        bad = projects_dir / "invalid.yaml"
        bad.write_text("owner: ''\nrepo: spark\n")

        configs = load_project_configs(projects_dir)
        assert len(configs) == 1
        assert configs[0].owner == "apache"


class TestConvenienceFunctions:
    def test_get_operator_config(self, tmp_config_dir: Path, settings: object) -> None:
        settings.FRANK_OPERATOR_CONFIG = str(tmp_config_dir / "operator.yaml")  # type: ignore[attr-defined]
        config = get_operator_config()
        assert config.github_username == "testuser"

    def test_get_project_config_by_name(self, tmp_config_dir: Path, settings: object) -> None:
        settings.FRANK_PROJECTS_DIR = str(tmp_config_dir / "projects")  # type: ignore[attr-defined]
        config = get_project_config("testorg-testrepo")
        assert config is not None
        assert config.owner == "testorg"

    def test_get_project_config_by_full_name(self, tmp_config_dir: Path, settings: object) -> None:
        settings.FRANK_PROJECTS_DIR = str(tmp_config_dir / "projects")  # type: ignore[attr-defined]
        config = get_project_config("testorg/testrepo")
        assert config is not None
        assert config.repo == "testrepo"

    def test_get_project_config_not_found(self, tmp_config_dir: Path, settings: object) -> None:
        settings.FRANK_PROJECTS_DIR = str(tmp_config_dir / "projects")  # type: ignore[attr-defined]
        config = get_project_config("nonexistent")
        assert config is None


class TestValidateYamlFile:
    def test_valid_operator_yaml(self, tmp_config_dir: Path) -> None:
        errors = validate_yaml_file(tmp_config_dir / "operator.yaml", "operator")
        assert errors == []

    def test_valid_project_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "project.yaml"
        f.write_text("owner: apache\nrepo: spark\n")
        errors = validate_yaml_file(f, "project")
        assert errors == []

    def test_missing_file(self, tmp_path: Path) -> None:
        errors = validate_yaml_file(tmp_path / "nope.yaml", "operator")
        assert len(errors) == 1
        assert "not found" in errors[0].lower()

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text(": invalid: yaml: [")
        errors = validate_yaml_file(f, "operator")
        assert len(errors) == 1
        assert "yaml" in errors[0].lower()

    def test_invalid_operator_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text("poll_interval_seconds: -5\n")
        errors = validate_yaml_file(f, "operator")
        assert len(errors) >= 1

    def test_invalid_project_yaml_missing_required(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text("tone: friendly\n")  # missing owner and repo
        errors = validate_yaml_file(f, "project")
        assert len(errors) >= 1

    def test_not_a_mapping(self, tmp_path: Path) -> None:
        f = tmp_path / "list.yaml"
        f.write_text("- item1\n- item2\n")
        errors = validate_yaml_file(f, "operator")
        assert any("mapping" in e.lower() for e in errors)


# --- v1.5 Config Models ---


class TestJiraConfig:
    def test_defaults(self) -> None:
        from franktheunicorn.config.models import JiraConfig

        config = JiraConfig()
        assert config.enabled is False
        assert config.server == ""
        assert config.project_prefix == ""

    def test_valid_server(self) -> None:
        from franktheunicorn.config.models import JiraConfig

        config = JiraConfig(
            enabled=True, server="https://issues.apache.org/jira", project_prefix="SPARK"
        )
        assert config.server == "https://issues.apache.org/jira"
        assert config.project_prefix == "SPARK"

    def test_trailing_slash_stripped(self) -> None:
        from franktheunicorn.config.models import JiraConfig

        config = JiraConfig(server="https://issues.apache.org/jira/")
        assert config.server == "https://issues.apache.org/jira"

    def test_invalid_server_no_scheme(self) -> None:
        from franktheunicorn.config.models import JiraConfig

        with pytest.raises(ValidationError, match="URL"):
            JiraConfig(server="issues.apache.org/jira")


class TestCommunitySourceConfig:
    def test_mailing_list(self) -> None:
        from franktheunicorn.config.models import CommunitySourceConfig

        config = CommunitySourceConfig(
            type="mailing-list",
            name="Spark dev@",
            archive_url="https://lists.apache.org/list.html?dev@spark.apache.org",
        )
        assert config.type == "mailing-list"
        assert config.timeout_seconds == 30
        assert config.cache_ttl_days == 7
        assert config.niceness_delay_seconds == 2.0

    def test_discord(self) -> None:
        from franktheunicorn.config.models import CommunitySourceConfig

        config = CommunitySourceConfig(
            type="discord",
            name="Spark Discord",
            guild_id="123456789",
            bot_token_env="DISCORD_BOT_TOKEN",
        )
        assert config.guild_id == "123456789"

    def test_unknown_type_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        from franktheunicorn.config.models import CommunitySourceConfig

        with caplog.at_level(logging.WARNING):
            config = CommunitySourceConfig(type="unknown-source")
        assert config.type == "unknown-source"
        assert "Unknown community source type" in caplog.text

    def test_zero_timeout_rejected(self) -> None:
        from franktheunicorn.config.models import CommunitySourceConfig

        with pytest.raises(ValidationError, match="timeout_seconds"):
            CommunitySourceConfig(type="mailing-list", timeout_seconds=0)


class TestDownstreamConfig:
    def test_basic(self) -> None:
        from franktheunicorn.config.models import DownstreamConfig

        config = DownstreamConfig(
            project="spark-testing-base",
            repo="holdenk/spark-testing-base",
            tracked_apis_file="data/cache/spark-testing-base-imports.json",
        )
        assert config.project == "spark-testing-base"
        assert config.repo == "holdenk/spark-testing-base"


class TestPostingConfig:
    def test_defaults_to_draft_only(self) -> None:
        from franktheunicorn.config.models import PostingConfig

        config = PostingConfig()
        assert config.mode == "draft-only"
        assert config.confidence_threshold == 0.85
        assert config.bot_token_env == "GITHUB_TOKEN_BOT"

    def test_confidence_gated(self) -> None:
        from franktheunicorn.config.models import PostingConfig

        config = PostingConfig(mode="confidence-gated", confidence_threshold=0.9)
        assert config.mode == "confidence-gated"
        assert config.confidence_threshold == 0.9

    def test_invalid_mode(self) -> None:
        from franktheunicorn.config.models import PostingConfig

        with pytest.raises(ValidationError, match="posting mode"):
            PostingConfig(mode="auto")

    def test_threshold_out_of_range(self) -> None:
        from franktheunicorn.config.models import PostingConfig

        with pytest.raises(ValidationError, match="confidence_threshold"):
            PostingConfig(confidence_threshold=1.5)


class TestSentryConfig:
    def test_defaults(self) -> None:
        from franktheunicorn.config.models import SentryConfig

        config = SentryConfig()
        assert config.enabled is False
        assert config.auth_token_env == "SENTRY_AUTH_TOKEN"
        assert config.score_weight == 15

    def test_configured(self) -> None:
        from franktheunicorn.config.models import SentryConfig

        config = SentryConfig(enabled=True, org_slug="myorg", project_slug="myproject")
        assert config.org_slug == "myorg"


class TestPerplexityConfig:
    def test_defaults(self) -> None:
        from franktheunicorn.config.models import PerplexityConfig

        config = PerplexityConfig()
        assert config.enabled is False
        assert config.mode == "both"

    def test_valid_modes(self) -> None:
        from franktheunicorn.config.models import PerplexityConfig

        for mode in ("general", "technical", "both"):
            config = PerplexityConfig(mode=mode)
            assert config.mode == mode

    def test_invalid_mode(self) -> None:
        from franktheunicorn.config.models import PerplexityConfig

        with pytest.raises(ValidationError, match="Perplexity mode"):
            PerplexityConfig(mode="invalid")


class TestProjectConfigV15:
    def test_v15_defaults(self) -> None:
        config = ProjectConfig(owner="apache", repo="spark")
        assert config.jira.enabled is False
        assert config.community_sources == []
        assert config.downstream == []
        assert config.posting.mode == "draft-only"

    def test_with_jira(self) -> None:
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            jira={
                "enabled": True,
                "server": "https://issues.apache.org/jira",
                "project_prefix": "SPARK",
            },
        )
        assert config.jira.enabled is True
        assert config.jira.project_prefix == "SPARK"

    def test_with_community_sources(self) -> None:
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            community_sources=[
                {"type": "mailing-list", "name": "dev@", "archive_url": "https://lists.apache.org"},
                {"type": "discourse", "name": "Forum", "base_url": "https://forum.example.com"},
            ],
        )
        assert len(config.community_sources) == 2
        assert config.community_sources[0].type == "mailing-list"
        assert config.community_sources[1].type == "discourse"

    def test_with_downstream(self) -> None:
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            downstream=[
                {"project": "spark-testing-base", "repo": "holdenk/spark-testing-base"},
            ],
        )
        assert len(config.downstream) == 1
        assert config.downstream[0].project == "spark-testing-base"

    def test_with_confidence_gated_posting(self) -> None:
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            posting={"mode": "confidence-gated", "confidence_threshold": 0.9},
        )
        assert config.posting.mode == "confidence-gated"
        assert config.posting.confidence_threshold == 0.9


class TestOperatorConfigV15:
    def test_sentry_defaults(self) -> None:
        config = OperatorConfig()
        assert config.sentry.enabled is False

    def test_perplexity_defaults(self) -> None:
        config = OperatorConfig()
        assert config.perplexity.enabled is False

    def test_with_sentry(self) -> None:
        config = OperatorConfig(
            sentry={"enabled": True, "org_slug": "myorg", "project_slug": "myproject"},
        )
        assert config.sentry.enabled is True

    def test_with_perplexity(self) -> None:
        config = OperatorConfig(
            perplexity={"enabled": True, "mode": "technical"},
        )
        assert config.perplexity.mode == "technical"


# --- v2 Config Models ---


class TestFineTuningConfig:
    def test_defaults(self) -> None:
        from franktheunicorn.config.models import FineTuningConfig

        config = FineTuningConfig()
        assert config.enabled is False
        assert config.default_base_model == "Qwen/Qwen2.5-Coder-7B-Instruct"
        assert config.quantization == "qlora-4bit"
        assert config.target_hardware == "3090"
        assert config.auto_schedule.enabled is False
        assert config.dataset_refresh.enabled is True

    def test_enabled_with_custom_model(self) -> None:
        from franktheunicorn.config.models import FineTuningConfig

        config = FineTuningConfig(
            enabled=True,
            default_base_model="mistralai/Mistral-7B-v0.3",
            quantization="qlora-8bit",
        )
        assert config.enabled is True
        assert config.default_base_model == "mistralai/Mistral-7B-v0.3"
        assert config.quantization == "qlora-8bit"

    def test_unknown_quantization_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        from franktheunicorn.config.models import FineTuningConfig

        with caplog.at_level(logging.WARNING):
            config = FineTuningConfig(quantization="unknown-mode")
        assert config.quantization == "unknown-mode"
        assert "Unknown quantization mode" in caplog.text

    def test_auto_schedule_with_values(self) -> None:
        from franktheunicorn.config.models import AutoScheduleConfig, FineTuningConfig

        config = FineTuningConfig(
            auto_schedule=AutoScheduleConfig(
                enabled=True,
                check_frequency="daily",
                min_new_actions=100,
            )
        )
        assert config.auto_schedule.enabled is True
        assert config.auto_schedule.check_frequency == "daily"
        assert config.auto_schedule.min_new_actions == 100

    def test_auto_schedule_invalid_frequency(self) -> None:
        from franktheunicorn.config.models import AutoScheduleConfig

        with pytest.raises(ValidationError, match="check_frequency"):
            AutoScheduleConfig(check_frequency="hourly")

    def test_auto_schedule_invalid_min_actions(self) -> None:
        from franktheunicorn.config.models import AutoScheduleConfig

        with pytest.raises(ValidationError, match="min_new_actions"):
            AutoScheduleConfig(min_new_actions=0)

    def test_dataset_refresh_invalid_frequency(self) -> None:
        from franktheunicorn.config.models import DatasetRefreshConfig

        with pytest.raises(ValidationError, match="frequency"):
            DatasetRefreshConfig(frequency="hourly")

    def test_operator_config_has_fine_tuning(self) -> None:
        config = OperatorConfig()
        assert config.fine_tuning.enabled is False
        assert config.fine_tuning.default_base_model == "Qwen/Qwen2.5-Coder-7B-Instruct"

    def test_operator_config_from_dict(self) -> None:
        config = OperatorConfig(
            fine_tuning={
                "enabled": True,
                "default_base_model": "mistralai/Mistral-7B-v0.3",
                "auto_schedule": {"enabled": True, "check_frequency": "monthly"},
            },
        )
        assert config.fine_tuning.enabled is True
        assert config.fine_tuning.auto_schedule.check_frequency == "monthly"


class TestFineTunedModelConfig:
    def test_defaults(self) -> None:
        from franktheunicorn.config.models import FineTunedModelConfig

        config = FineTunedModelConfig()
        assert config.enabled is False
        assert config.provider == "ollama"
        assert config.endpoint == "http://localhost:11434"
        assert config.slot == "first-pass"
        assert config.refine_with == "primary"

    def test_configured(self) -> None:
        from franktheunicorn.config.models import FineTunedModelConfig

        config = FineTunedModelConfig(
            enabled=True,
            provider="vllm",
            model="franktheunicorn-spark-v1",
            endpoint="http://localhost:8000",
            slot="primary",
        )
        assert config.enabled is True
        assert config.provider == "vllm"
        assert config.model == "franktheunicorn-spark-v1"

    def test_unknown_provider_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        from franktheunicorn.config.models import FineTunedModelConfig

        with caplog.at_level(logging.WARNING):
            config = FineTunedModelConfig(provider="unknown-provider")
        assert config.provider == "unknown-provider"
        assert "Unknown fine-tuned model provider" in caplog.text

    def test_invalid_slot_rejected(self) -> None:
        from franktheunicorn.config.models import FineTunedModelConfig

        with pytest.raises(ValidationError, match="slot"):
            FineTunedModelConfig(slot="invalid-slot")

    def test_project_config_has_fine_tuned_model(self) -> None:
        config = ProjectConfig(owner="apache", repo="spark")
        assert config.fine_tuned_model.enabled is False

    def test_project_config_from_dict(self) -> None:
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            fine_tuned_model={
                "enabled": True,
                "provider": "ollama",
                "model": "franktheunicorn-spark-v1",
            },
        )
        assert config.fine_tuned_model.enabled is True
        assert config.fine_tuned_model.model == "franktheunicorn-spark-v1"


class TestMergeQueueConfig:
    def test_defaults(self) -> None:
        from franktheunicorn.config.models import MergeQueueConfig

        config = MergeQueueConfig()
        assert config.enabled is False
        assert config.required_approvals == 1
        assert config.require_ci_pass is True
        assert config.require_no_conflicts is True
        assert config.merge_script == ""
        assert config.auto_merge is False
        assert config.merge_method == "merge"

    def test_configured_with_merge_script(self) -> None:
        from franktheunicorn.config.models import MergeQueueConfig

        config = MergeQueueConfig(
            enabled=True,
            required_approvals=2,
            merge_script="/path/to/merge_spark_pr.py",
            merge_method="squash",
        )
        assert config.enabled is True
        assert config.required_approvals == 2
        assert config.merge_script == "/path/to/merge_spark_pr.py"
        assert config.merge_method == "squash"

    def test_negative_approvals_rejected(self) -> None:
        from franktheunicorn.config.models import MergeQueueConfig

        with pytest.raises(ValidationError, match="required_approvals"):
            MergeQueueConfig(required_approvals=-1)

    def test_zero_approvals_accepted(self) -> None:
        from franktheunicorn.config.models import MergeQueueConfig

        config = MergeQueueConfig(required_approvals=0)
        assert config.required_approvals == 0

    def test_invalid_merge_method(self) -> None:
        from franktheunicorn.config.models import MergeQueueConfig

        with pytest.raises(ValidationError, match="merge_method"):
            MergeQueueConfig(merge_method="fast-forward")

    def test_restack_defaults_and_legacy_flag(self) -> None:
        from franktheunicorn.config.models import MergeQueueConfig

        default_config = MergeQueueConfig()
        assert default_config.restack_enabled is False
        assert default_config.restack_target_branch == "main"
        assert default_config.migration_globs == ["*/migrations/*.py"]
        assert default_config.delete_stale_migrations is False
        assert default_config.ci_wait_timeout_seconds == 900
        assert default_config.ci_poll_interval_seconds == 30
        assert default_config.push_force_with_lease is True

        legacy_config = MergeQueueConfig(post_merge_restack_enabled=True)
        assert legacy_config.restack_enabled is True

    @pytest.mark.parametrize("timeout", [59, 7201])
    def test_restack_ci_timeout_bounds(self, timeout: int) -> None:
        from franktheunicorn.config.models import MergeQueueConfig

        with pytest.raises(ValidationError, match="ci_wait_timeout_seconds"):
            MergeQueueConfig(ci_wait_timeout_seconds=timeout)

    @pytest.mark.parametrize("poll", [4, 301])
    def test_restack_ci_poll_bounds(self, poll: int) -> None:
        from franktheunicorn.config.models import MergeQueueConfig

        with pytest.raises(ValidationError, match="ci_poll_interval_seconds"):
            MergeQueueConfig(ci_poll_interval_seconds=poll, ci_wait_timeout_seconds=900)

    def test_restack_ci_poll_must_be_less_than_timeout(self) -> None:
        from franktheunicorn.config.models import MergeQueueConfig

        with pytest.raises(ValidationError, match="must be lower"):
            MergeQueueConfig(ci_poll_interval_seconds=60, ci_wait_timeout_seconds=60)

    def test_project_config_has_merge_queue(self) -> None:
        config = ProjectConfig(owner="apache", repo="spark")
        assert config.merge_queue.enabled is False

    def test_project_config_from_dict(self) -> None:
        config = ProjectConfig(
            owner="apache",
            repo="spark",
            merge_queue={
                "enabled": True,
                "required_approvals": 2,
                "merge_script": "/opt/merge_spark_pr.py",
            },
        )
        assert config.merge_queue.enabled is True
        assert config.merge_queue.merge_script == "/opt/merge_spark_pr.py"
