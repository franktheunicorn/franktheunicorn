"""Tests for config resolver and ${ENV_VAR} expansion."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from franktheunicorn.config.loader import _expand_env_vars
from franktheunicorn.config.resolver import (
    resolve_config,
    resolve_operator_config_path,
)


class TestExpandEnvVars:
    def test_simple_substitution(self) -> None:
        with patch.dict("os.environ", {"MY_VAR": "hello"}):
            assert _expand_env_vars("${MY_VAR}") == "hello"

    def test_partial_substitution(self) -> None:
        with patch.dict("os.environ", {"HOME": "/home/user"}):
            assert _expand_env_vars("${HOME}/frank-data") == "/home/user/frank-data"

    def test_missing_var_becomes_empty(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert _expand_env_vars("${NONEXISTENT}") == ""

    def test_no_expansion_for_plain_strings(self) -> None:
        assert _expand_env_vars("hello world") == "hello world"

    def test_nested_dict(self) -> None:
        with patch.dict("os.environ", {"TOKEN": "abc123"}):
            data = {"outer": {"inner": "${TOKEN}"}}
            result = _expand_env_vars(data)
            assert result == {"outer": {"inner": "abc123"}}

    def test_list(self) -> None:
        with patch.dict("os.environ", {"A": "x", "B": "y"}):
            data = ["${A}", "${B}", "literal"]
            result = _expand_env_vars(data)
            assert result == ["x", "y", "literal"]

    def test_non_string_values_unchanged(self) -> None:
        assert _expand_env_vars(42) == 42
        assert _expand_env_vars(True) is True
        assert _expand_env_vars(None) is None
        assert _expand_env_vars(3.14) == 3.14

    def test_multiple_vars_in_one_string(self) -> None:
        with patch.dict("os.environ", {"A": "hello", "B": "world"}):
            assert _expand_env_vars("${A} ${B}") == "hello world"

    def test_mixed_dict(self) -> None:
        with patch.dict("os.environ", {"TOKEN": "secret"}):
            data = {
                "name": "plain",
                "token": "${TOKEN}",
                "count": 5,
                "enabled": True,
            }
            result = _expand_env_vars(data)
            assert result["name"] == "plain"
            assert result["token"] == "secret"
            assert result["count"] == 5
            assert result["enabled"] is True


class TestResolveOperatorConfigPath:
    def test_prefers_active_config(self, tmp_path: Path) -> None:
        active = tmp_path / "config" / "active" / "operator.yaml"
        active.parent.mkdir(parents=True)
        active.write_text("mock_mode: true\n")

        examples = tmp_path / "config" / "examples" / "operator.yaml"
        examples.parent.mkdir(parents=True)
        examples.write_text("mock_mode: false\n")

        result = resolve_operator_config_path(tmp_path)
        assert result == str(active)

    def test_falls_back_to_examples(self, tmp_path: Path) -> None:
        examples = tmp_path / "config" / "examples" / "operator.yaml"
        examples.parent.mkdir(parents=True)
        examples.write_text("mock_mode: false\n")

        result = resolve_operator_config_path(tmp_path)
        assert result == str(examples)

    def test_returns_examples_path_even_if_missing(self, tmp_path: Path) -> None:
        result = resolve_operator_config_path(tmp_path)
        assert "config/examples/operator.yaml" in result


class TestResolveConfig:
    def test_defaults_when_no_config_exists(self, tmp_path: Path) -> None:
        _oc, resolved = resolve_config(tmp_path)
        assert resolved["mock_mode"] is False
        assert resolved["data_dir"] == str(tmp_path / "data")
        assert resolved["github_token"] == ""
        assert resolved["poll_interval"] == 300
        assert resolved["email_host"] == ""
        assert resolved["email_port"] == 587
        assert resolved["email_from"] == "frank@localhost"

    def test_yaml_values_used(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text(
            "mock_mode: true\n"
            "poll_interval_seconds: 60\n"
            'data_dir: "/custom/data"\n'
            'digest_email: "me@example.com"\n'
        )

        _oc, resolved = resolve_config(tmp_path)
        assert resolved["mock_mode"] is True
        assert resolved["poll_interval"] == 60
        assert resolved["data_dir"] == "/custom/data"
        assert resolved["digest_email"] == "me@example.com"

    def test_env_var_expansion_in_yaml(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text('github_token: "${TEST_GH_TOKEN}"\n')

        with patch.dict("os.environ", {"TEST_GH_TOKEN": "ghp_secret123"}):
            _oc, resolved = resolve_config(tmp_path)
        assert resolved["github_token"] == "ghp_secret123"

    def test_email_config_from_yaml(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text(
            "email:\n"
            '  smtp_host: "smtp.example.com"\n'
            "  smtp_port: 465\n"
            '  from_address: "frank@example.com"\n'
        )

        _oc, resolved = resolve_config(tmp_path)
        assert resolved["email_host"] == "smtp.example.com"
        assert resolved["email_port"] == 465
        assert resolved["email_from"] == "frank@example.com"

    def test_projects_dir_fallback(self, tmp_path: Path) -> None:
        # Create examples projects dir with a yaml file
        examples_projects = tmp_path / "config" / "examples" / "projects"
        examples_projects.mkdir(parents=True)
        (examples_projects / "test.yaml").write_text("owner: x\nrepo: y\n")

        _oc, resolved = resolve_config(tmp_path)
        assert resolved["projects_dir"] == str(examples_projects)

    def test_projects_dir_from_yaml(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text('projects_dir: "/custom/projects"\n')

        _oc, resolved = resolve_config(tmp_path)
        assert resolved["projects_dir"] == "/custom/projects"

    def test_log_level_default_is_info(self, tmp_path: Path) -> None:
        _oc, resolved = resolve_config(tmp_path)
        assert resolved["log_level"] == "INFO"

    def test_log_level_from_yaml(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text('log_level: "DEBUG"\n')

        oc, resolved = resolve_config(tmp_path)
        assert oc.log_level == "DEBUG"
        assert resolved["log_level"] == "DEBUG"

    def test_log_level_normalised_to_uppercase(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text('log_level: "debug"\n')

        _oc, resolved = resolve_config(tmp_path)
        assert resolved["log_level"] == "DEBUG"

    def test_log_level_env_var_overrides_yaml(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text('log_level: "INFO"\n')

        with patch.dict("os.environ", {"FRANK_LOG_LEVEL": "DEBUG"}):
            _oc, resolved = resolve_config(tmp_path)
        assert resolved["log_level"] == "DEBUG"

    def test_log_level_env_var_invalid_falls_back_to_yaml(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text('log_level: "WARNING"\n')

        with patch.dict("os.environ", {"FRANK_LOG_LEVEL": "BANANAS"}):
            _oc, resolved = resolve_config(tmp_path)
        assert resolved["log_level"] == "WARNING"

    def test_backwards_compat_no_new_fields(self, tmp_path: Path) -> None:
        """Config files without new fields still load with sensible defaults."""
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text(
            'github_username: "testuser"\nreview_style: "direct"\npoll_interval_seconds: 120\n'
        )

        oc, resolved = resolve_config(tmp_path)
        assert oc.github_username == "testuser"
        assert resolved["mock_mode"] is False
        assert resolved["github_token"] == ""
        assert resolved["poll_interval"] == 120


class TestResolveConfigUsernameInference:
    def test_infers_username_when_token_present(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text(
            'github_token: "ghp_test_token"\ngithub_username: ""\n'
        )
        with patch(
            "franktheunicorn.backends.github.infer_github_username",
            return_value="inferred-user",
        ):
            oc, _resolved = resolve_config(tmp_path)
        assert oc.github_username == "inferred-user"

    def test_skips_inference_when_username_set(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text(
            'github_token: "ghp_test_token"\ngithub_username: "explicit-user"\n'
        )
        with patch(
            "franktheunicorn.backends.github.infer_github_username",
        ) as mock_infer:
            _oc, _resolved = resolve_config(tmp_path)
        mock_infer.assert_not_called()
        assert _oc.github_username == "explicit-user"

    def test_skips_inference_in_mock_mode(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text(
            'github_token: "ghp_test_token"\nmock_mode: true\n'
        )
        with patch(
            "franktheunicorn.backends.github.infer_github_username",
        ) as mock_infer:
            _oc, _resolved = resolve_config(tmp_path)
        mock_infer.assert_not_called()

    def test_skips_inference_when_no_token(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text('github_username: ""\n')
        with patch(
            "franktheunicorn.backends.github.infer_github_username",
        ) as mock_infer:
            _oc, _resolved = resolve_config(tmp_path)
        mock_infer.assert_not_called()

    def test_continues_with_empty_username_on_inference_failure(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "operator.yaml").write_text('github_token: "ghp_test_token"\n')
        with patch(
            "franktheunicorn.backends.github.infer_github_username",
            return_value="",
        ):
            oc, _resolved = resolve_config(tmp_path)
        assert oc.github_username == ""
