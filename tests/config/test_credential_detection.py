"""Tests for franktheunicorn.config.credential_detection."""

from __future__ import annotations

import pytest

from franktheunicorn.config.credential_detection import (
    DetectedCredential,
    build_dynamic_menu_entries,
    derive_detection_label,
    detect_llm_credentials,
    format_detections,
    get_openai_compatible_detections,
    mask_value,
    suggest_provider_choices,
)


class TestMaskValue:
    def test_normal_value(self) -> None:
        assert mask_value("sk-ant-api03-abc123") == "sk-ant****"

    def test_short_value(self) -> None:
        assert mask_value("ab") == "****"

    def test_exactly_four_chars(self) -> None:
        assert mask_value("abcd") == "****"

    def test_five_chars(self) -> None:
        assert mask_value("abcde") == "abcde****"

    def test_six_chars(self) -> None:
        assert mask_value("abcdef") == "abcdef****"

    def test_empty_string(self) -> None:
        assert mask_value("") == "****"


class TestTier1Detection:
    def test_anthropic_key_detected(self) -> None:
        env = {"ANTHROPIC_API_KEY": "sk-ant-api03-test123"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].env_var == "ANTHROPIC_API_KEY"
        assert results[0].provider == "claude"
        assert results[0].confidence == "high"
        assert results[0].credential_type == "api_key"

    def test_openai_key_detected(self) -> None:
        env = {"OPENAI_API_KEY": "sk-proj-test123"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].provider == "openai"
        assert results[0].confidence == "high"

    def test_google_key_detected(self) -> None:
        env = {"GOOGLE_API_KEY": "AIzaSyTestKey123"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].provider == "gemini"
        assert results[0].confidence == "high"

    def test_multiple_tier1_keys(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "OPENAI_API_KEY": "sk-proj-test",
        }
        results = detect_llm_credentials(env)
        high = [r for r in results if r.confidence == "high"]
        assert len(high) == 2
        providers = {r.provider for r in high}
        assert providers == {"claude", "openai"}

    def test_empty_value_skipped(self) -> None:
        env = {"ANTHROPIC_API_KEY": ""}
        results = detect_llm_credentials(env)
        assert len(results) == 0

    def test_github_token_not_included_by_default(self) -> None:
        env = {"GITHUB_TOKEN": "ghp_test123"}
        results = detect_llm_credentials(env)
        assert len(results) == 0

    def test_github_token_included_when_requested(self) -> None:
        env = {"GITHUB_TOKEN": "ghp_test123"}
        results = detect_llm_credentials(env, include_github=True)
        assert len(results) == 1
        assert results[0].provider == "github"
        assert results[0].confidence == "high"

    def test_gh_token_included_when_requested(self) -> None:
        env = {"GH_TOKEN": "ghp_test123"}
        results = detect_llm_credentials(env, include_github=True)
        assert len(results) == 1
        assert results[0].provider == "github"


class TestTier2Detection:
    def test_mistral_key(self) -> None:
        env = {"MISTRAL_API_KEY": "test-key-123"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].provider == "mistral"
        assert results[0].confidence == "medium"

    def test_groq_key(self) -> None:
        env = {"GROQ_API_KEY": "gsk_test123"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].provider == "groq"
        assert results[0].confidence == "medium"

    def test_deepseek_key(self) -> None:
        env = {"DEEPSEEK_API_KEY": "sk-deepseek-test"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].provider == "deepseek"

    def test_azure_openai_key(self) -> None:
        env = {"AZURE_OPENAI_API_KEY": "azure-key-123"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].provider == "azure-openai"
        assert results[0].confidence == "medium"

    def test_hf_token(self) -> None:
        env = {"HF_TOKEN": "hf_test123"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].provider == "huggingface"

    def test_together_key(self) -> None:
        env = {"TOGETHER_API_KEY": "key-123"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].provider == "together"

    def test_ollama_host_detected(self) -> None:
        """OLLAMA_HOST is recognised as a known local-inference endpoint."""
        env = {"OLLAMA_HOST": "http://localhost:11434"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].provider == "ollama"
        assert results[0].confidence == "medium"
        assert results[0].credential_type == "endpoint"

    def test_ollama_base_url_detected(self) -> None:
        """OLLAMA_BASE_URL is recognised as a known local-inference endpoint."""
        env = {"OLLAMA_BASE_URL": "http://my-ollama:11434"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].provider == "ollama"
        assert results[0].confidence == "medium"
        assert results[0].credential_type == "endpoint"

    def test_llama_cpp_host_detected(self) -> None:
        env = {"LLAMA_CPP_HOST": "http://localhost:8080"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].provider == "llama-cpp"

    def test_vllm_host_detected(self) -> None:
        env = {"VLLM_HOST": "http://localhost:8000"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].provider == "vllm"


class TestTier3Detection:
    def test_endpoint_with_v1_detected(self) -> None:
        env = {"MY_LLM_URL": "https://my-server.com/api/v1"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].confidence == "low"
        assert results[0].credential_type == "endpoint"

    def test_endpoint_with_chat_completions(self) -> None:
        env = {"CUSTOM_AI_ENDPOINT": "https://ai.example.com/chat/completions"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].credential_type == "endpoint"
        assert results[0].confidence == "low"

    def test_endpoint_with_api_v1(self) -> None:
        env = {"LLM_BASE_URL": "http://localhost:8080/api/llm/v1"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].credential_type == "endpoint"

    def test_endpoint_with_port_only_detected(self) -> None:
        """URLs with explicit port numbers (and no /v1 path) are now detected.

        Local-inference servers like a custom Ollama or llama.cpp deployment
        often expose their bare URL — broadening the regex lets us pick them
        up even when the URL doesn't include /v1 or /chat/completions.
        """
        env = {"MY_INFERENCE_HOST": "http://localhost:11434"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].credential_type == "endpoint"

    def test_endpoint_with_remote_port_detected(self) -> None:
        env = {"CUSTOM_LLM_URL": "https://my-server.example.com:8443"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].credential_type == "endpoint"

    def test_postgres_url_still_not_detected(self) -> None:
        """Non-http URLs with ports are still excluded."""
        env = {"DATABASE_URL": "postgres://localhost:5432/db"}
        results = detect_llm_credentials(env)
        assert len(results) == 0

    def test_endpoint_paired_with_key(self) -> None:
        env = {
            "MY_LLM_URL": "https://my-server.com/api/v1",
            "MY_LLM_KEY": "sk-secret-123",
        }
        results = detect_llm_credentials(env)
        assert len(results) == 2
        endpoint = next(r for r in results if r.credential_type == "endpoint")
        key = next(r for r in results if r.credential_type == "api_key")
        assert endpoint.paired_with == "MY_LLM_KEY"
        assert key.paired_with == "MY_LLM_URL"

    def test_endpoint_paired_with_api_key_suffix(self) -> None:
        env = {
            "ACME_URL": "https://acme.ai/api/v1",
            "ACME_API_KEY": "sk-acme-123",
        }
        results = detect_llm_credentials(env)
        endpoint = next(r for r in results if r.credential_type == "endpoint")
        assert endpoint.paired_with == "ACME_API_KEY"

    def test_endpoint_paired_with_token_suffix(self) -> None:
        env = {
            "ACME_URL": "https://acme.ai/v1",
            "ACME_TOKEN": "tok-123",
        }
        results = detect_llm_credentials(env)
        endpoint = next(r for r in results if r.credential_type == "endpoint")
        assert endpoint.paired_with == "ACME_TOKEN"

    def test_endpoint_paired_with_pat_suffix(self) -> None:
        env = {
            "ACME_URL": "https://acme.ai/api/v1",
            "ACME_PAT": "pat-123",
        }
        results = detect_llm_credentials(env)
        endpoint = next(r for r in results if r.credential_type == "endpoint")
        assert endpoint.paired_with == "ACME_PAT"

    def test_endpoint_paired_with_secret_suffix(self) -> None:
        env = {
            "ACME_URL": "https://acme.ai/api/v1",
            "ACME_SECRET": "secret-123",
        }
        results = detect_llm_credentials(env)
        endpoint = next(r for r in results if r.credential_type == "endpoint")
        assert endpoint.paired_with == "ACME_SECRET"

    def test_standalone_api_key_with_known_prefix(self) -> None:
        env = {"CUSTOM_AI_API_KEY": "sk-custom-12345"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].confidence == "low"
        assert results[0].credential_type == "api_key"

    def test_standalone_api_token_with_known_prefix(self) -> None:
        env = {"MY_SERVICE_API_TOKEN": "key-abc123"}
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].confidence == "low"

    def test_non_llm_url_not_detected(self) -> None:
        env = {"DATABASE_URL": "postgres://localhost:5432/db"}
        results = detect_llm_credentials(env)
        assert len(results) == 0

    def test_excluded_prefixes_not_detected(self) -> None:
        env = {"SSH_AUTH_SOCK": "/tmp/ssh.sock"}
        results = detect_llm_credentials(env)
        assert len(results) == 0

    def test_url_suffix_variations(self) -> None:
        for suffix in ("_URL", "_BASE_URL", "_ENDPOINT", "_HOST", "_BASE"):
            env = {f"MY_LLM{suffix}": "https://llm.example.com/api/v1"}
            results = detect_llm_credentials(env)
            assert len(results) >= 1, f"Failed for suffix {suffix}"
            assert results[0].credential_type == "endpoint"

    def test_api_key_without_known_prefix_not_detected(self) -> None:
        env = {"CUSTOM_API_KEY": "just-a-random-string"}
        results = detect_llm_credentials(env)
        assert len(results) == 0


class TestDeduplication:
    def test_tier1_not_reemitted_at_tier3(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "sk-ant-test123",
        }
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].confidence == "high"

    def test_tier2_not_reemitted_at_tier3(self) -> None:
        env = {
            "GROQ_API_KEY": "gsk_test123",
        }
        results = detect_llm_credentials(env)
        assert len(results) == 1
        assert results[0].confidence == "medium"


class TestSortOrder:
    def test_high_before_medium_before_low(self) -> None:
        env = {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "GROQ_API_KEY": "gsk_test",
            "MY_LLM_URL": "https://llm.example.com/api/v1",
        }
        results = detect_llm_credentials(env)
        confidences = [r.confidence for r in results]
        assert confidences == sorted(
            confidences, key=lambda c: {"high": 0, "medium": 1, "low": 2}[c]
        )


class TestSuggestProviderChoices:
    def test_single_claude(self) -> None:
        detections = [
            DetectedCredential(
                env_var="ANTHROPIC_API_KEY",
                value_preview="sk-ant-****",
                provider="claude",
                confidence="high",
                credential_type="api_key",
                paired_with="",
            )
        ]
        assert suggest_provider_choices(detections) == "1"

    def test_single_openai(self) -> None:
        detections = [
            DetectedCredential(
                env_var="OPENAI_API_KEY",
                value_preview="sk-****",
                provider="openai",
                confidence="high",
                credential_type="api_key",
                paired_with="",
            )
        ]
        assert suggest_provider_choices(detections) == "2"

    def test_multiple_providers(self) -> None:
        detections = [
            DetectedCredential("ANTHROPIC_API_KEY", "sk-****", "claude", "high", "api_key", ""),
            DetectedCredential("OPENAI_API_KEY", "sk-****", "openai", "high", "api_key", ""),
        ]
        assert suggest_provider_choices(detections) == "1,2"

    def test_no_detections(self) -> None:
        assert suggest_provider_choices([]) == "7"

    def test_only_medium_confidence_returns_skip(self) -> None:
        detections = [
            DetectedCredential("GROQ_API_KEY", "gsk_****", "groq", "medium", "api_key", ""),
        ]
        assert suggest_provider_choices(detections) == "7"

    def test_only_low_confidence_returns_skip(self) -> None:
        detections = [
            DetectedCredential("MY_URL", "http****", "", "low", "endpoint", ""),
        ]
        assert suggest_provider_choices(detections) == "7"


class TestFormatDetections:
    def test_empty_returns_empty(self) -> None:
        assert format_detections([]) == ""

    def test_high_confidence_displayed(self) -> None:
        detections = [
            DetectedCredential("ANTHROPIC_API_KEY", "sk-ant-****", "claude", "high", "api_key", ""),
        ]
        output = format_detections(detections)
        assert "ANTHROPIC_API_KEY" in output
        assert "claude" in output
        assert "Found:" in output

    def test_medium_confidence_displayed(self) -> None:
        detections = [
            DetectedCredential("GROQ_API_KEY", "gsk_****", "groq", "medium", "api_key", ""),
        ]
        output = format_detections(detections)
        assert "GROQ_API_KEY" in output
        assert "OpenAI-compatible" in output

    def test_low_confidence_with_paired(self) -> None:
        detections = [
            DetectedCredential("MY_URL", "http****", "", "low", "endpoint", "MY_KEY"),
        ]
        output = format_detections(detections)
        assert "Possible matches:" in output
        assert "paired with MY_KEY" in output


class TestGetOpenaiCompatibleDetections:
    def test_filters_tier2_credentials(self) -> None:
        detections = [
            DetectedCredential("ANTHROPIC_API_KEY", "sk-****", "claude", "high", "api_key", ""),
            DetectedCredential("GROQ_API_KEY", "gsk_****", "groq", "medium", "api_key", ""),
        ]
        compat = get_openai_compatible_detections(detections)
        assert len(compat) == 1
        assert compat[0].provider == "groq"

    def test_excludes_native_providers(self) -> None:
        detections = [
            DetectedCredential("OPENAI_API_KEY", "sk-****", "openai", "high", "api_key", ""),
        ]
        compat = get_openai_compatible_detections(detections)
        assert len(compat) == 0

    def test_includes_low_confidence_keys(self) -> None:
        detections = [
            DetectedCredential("CUSTOM_API_KEY", "sk-****", "", "low", "api_key", ""),
        ]
        compat = get_openai_compatible_detections(detections)
        assert len(compat) == 1

    def test_includes_endpoints(self) -> None:
        detections = [
            DetectedCredential("MY_URL", "http****", "", "low", "endpoint", ""),
        ]
        compat = get_openai_compatible_detections(detections)
        assert len(compat) == 1
        assert compat[0].credential_type == "endpoint"


class TestDeriveDetectionLabel:
    def test_tier2_uses_provider_name(self) -> None:
        d = DetectedCredential("GROQ_API_KEY", "gsk_****", "groq", "medium", "api_key", "")
        assert derive_detection_label(d) == "groq"

    def test_tier2_mistral(self) -> None:
        d = DetectedCredential("MISTRAL_API_KEY", "key-****", "mistral", "medium", "api_key", "")
        assert derive_detection_label(d) == "mistral"

    def test_tier3_endpoint_strips_url_suffix(self) -> None:
        d = DetectedCredential(
            "PREPROD_CORTEX_URL", "http****", "", "low", "endpoint", "PREPROD_CORTEX_PAT"
        )
        assert derive_detection_label(d) == "preprod-cortex"

    def test_tier3_endpoint_strips_base_url_suffix(self) -> None:
        d = DetectedCredential("MY_LLM_BASE_URL", "http****", "", "low", "endpoint", "")
        assert derive_detection_label(d) == "my-llm"

    def test_tier3_api_key_strips_suffix(self) -> None:
        d = DetectedCredential("CUSTOM_AI_API_KEY", "sk-****", "", "low", "api_key", "")
        assert derive_detection_label(d) == "custom-ai"

    def test_tier3_api_token_strips_suffix(self) -> None:
        d = DetectedCredential("SVC_API_TOKEN", "key-****", "", "low", "api_key", "")
        assert derive_detection_label(d) == "svc"

    def test_tier3_endpoint_suffix(self) -> None:
        d = DetectedCredential("AI_ENDPOINT", "http****", "", "low", "endpoint", "")
        assert derive_detection_label(d) == "ai"

    def test_empty_provider_no_suffix_match(self) -> None:
        d = DetectedCredential("SOMETHING", "val****", "", "low", "api_key", "")
        assert derive_detection_label(d) == "something"


class TestBuildDynamicMenuEntries:
    def test_tier2_creates_entry(self) -> None:
        detections = [
            DetectedCredential("GROQ_API_KEY", "gsk_****", "groq", "medium", "api_key", ""),
        ]
        entries = build_dynamic_menu_entries(detections)
        assert len(entries) == 1
        assert entries[0].key == "8"
        assert entries[0].label == "groq"
        assert entries[0].api_key_env == "GROQ_API_KEY"
        assert entries[0].base_url_env == ""
        assert entries[0].provider_hint == "groq"

    def test_tier3_paired_creates_single_entry(self) -> None:
        detections = [
            DetectedCredential("MY_URL", "http****", "", "low", "endpoint", "MY_API_KEY"),
            DetectedCredential("MY_API_KEY", "sk-****", "", "low", "api_key", "MY_URL"),
        ]
        entries = build_dynamic_menu_entries(detections)
        assert len(entries) == 1
        assert entries[0].api_key_env == "MY_API_KEY"
        assert entries[0].base_url_env == "MY_URL"

    def test_tier1_excluded(self) -> None:
        detections = [
            DetectedCredential("ANTHROPIC_API_KEY", "sk-****", "claude", "high", "api_key", ""),
        ]
        entries = build_dynamic_menu_entries(detections)
        assert len(entries) == 0

    def test_multiple_entries_get_sequential_keys(self) -> None:
        detections = [
            DetectedCredential("GROQ_API_KEY", "gsk_****", "groq", "medium", "api_key", ""),
            DetectedCredential("MISTRAL_API_KEY", "key-****", "mistral", "medium", "api_key", ""),
        ]
        entries = build_dynamic_menu_entries(detections)
        assert len(entries) == 2
        assert entries[0].key == "8"
        assert entries[1].key == "9"

    def test_deduplicates_by_label(self) -> None:
        detections = [
            DetectedCredential("TOGETHER_API_KEY", "key-****", "together", "medium", "api_key", ""),
            DetectedCredential(
                "TOGETHER_AI_API_KEY", "key-****", "together", "medium", "api_key", ""
            ),
        ]
        entries = build_dynamic_menu_entries(detections)
        assert len(entries) == 1

    def test_empty_detections(self) -> None:
        assert build_dynamic_menu_entries([]) == []

    def test_custom_start_key(self) -> None:
        detections = [
            DetectedCredential("GROQ_API_KEY", "gsk_****", "groq", "medium", "api_key", ""),
        ]
        entries = build_dynamic_menu_entries(detections, start_key=10)
        assert entries[0].key == "10"

    def test_paired_credential_from_key_side(self) -> None:
        """When iterating hits the api_key first (paired with endpoint)."""
        detections = [
            DetectedCredential("ACME_API_KEY", "sk-****", "", "low", "api_key", "ACME_URL"),
            DetectedCredential("ACME_URL", "http****", "", "low", "endpoint", "ACME_API_KEY"),
        ]
        entries = build_dynamic_menu_entries(detections)
        assert len(entries) == 1
        assert entries[0].api_key_env == "ACME_API_KEY"
        assert entries[0].base_url_env == "ACME_URL"

    def test_azure_openai_pairs_by_provider(self) -> None:
        """Azure OpenAI has both endpoint and key in Tier 2 without paired_with."""
        detections = [
            DetectedCredential(
                "AZURE_OPENAI_API_KEY", "azure****", "azure-openai", "medium", "api_key", ""
            ),
            DetectedCredential(
                "AZURE_OPENAI_ENDPOINT", "https****", "azure-openai", "medium", "endpoint", ""
            ),
        ]
        entries = build_dynamic_menu_entries(detections)
        assert len(entries) == 1
        assert entries[0].label == "azure-openai"
        assert entries[0].api_key_env == "AZURE_OPENAI_API_KEY"
        assert entries[0].base_url_env == "AZURE_OPENAI_ENDPOINT"


class TestNoCredentials:
    def test_clean_env_returns_empty(self) -> None:
        env = {"HOME": "/home/user", "PATH": "/usr/bin", "LANG": "en_US.UTF-8"}
        results = detect_llm_credentials(env)
        assert results == []

    @pytest.mark.parametrize(
        "var",
        ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY"],
    )
    def test_empty_values_not_detected(self, var: str) -> None:
        env = {var: ""}
        results = detect_llm_credentials(env)
        assert results == []
